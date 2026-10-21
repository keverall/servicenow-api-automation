"""OpsRamp Cloud API client: OAuth2 token management and the JSON wrapper.

Base URL is ``{base_url}/api`` where ``base_url`` is either the public
``https://api.opsramp.com`` or a tenant subdomain such as
``https://acme.api.opsramp.com``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from ..config import OpsRampSettings
from ..errors import (
    OpsRampApiError,
    OpsRampAuthError,
    OpsRampNotFoundError,
    OpsRampRateLimitError,
)
from ..http import RetryPolicy, build_client, request_with_retry
from ..logging import get_logger

log = get_logger(__name__)

TOKEN_PATH = "/tenancy/auth/oauth/token"  # noqa: S105 - a path, not a secret
DEFAULT_SCOPE = "global:manage"
MAX_PAGES = 10_000


class OpsRampTokenManager:
    """Fetches and caches OpsRamp OAuth2 ``client_credentials`` tokens."""

    def __init__(
        self,
        client: httpx.Client,
        base_url: str,
        client_id: str,
        client_secret: str,
        scope: str = DEFAULT_SCOPE,
        refresh_margin_s: int = 60,
        policy: RetryPolicy | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._refresh_margin_s = refresh_margin_s
        self._policy = policy or RetryPolicy()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def token_url(self) -> str:
        return f"{self._base_url}{TOKEN_PATH}"

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-authenticates."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def access_token(self) -> str:
        """Return a valid access token, refreshing it when it is close to expiry."""
        with self._lock:
            if self._token and time.time() < (self._expires_at - self._refresh_margin_s):
                return self._token
            token, expires_in = self._fetch_token()
            self._token = token
            self._expires_at = time.time() + max(0.0, expires_in)
            return token

    def _fetch_token(self) -> tuple[str, float]:
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }
        response = self._post_token(form)
        if response.status_code in (400, 415, 422):
            # Some OpsRamp deployments only accept a JSON token body.
            response = self._post_token(form, json_body=True)
        if response.status_code in (400, 401, 403):
            raise OpsRampAuthError(
                "OpsRamp rejected the client credentials "
                f"(HTTP {response.status_code}): {_body_snippet(response)}"
            )
        if response.status_code >= 400:
            raise OpsRampAuthError(
                f"Unexpected response from the OpsRamp token endpoint (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpsRampAuthError("OpsRamp token endpoint returned a non-JSON body") from exc
        token = payload.get("access_token") or payload.get("token")
        if not token:
            raise OpsRampAuthError("OpsRamp token endpoint returned no access token")
        expires_in = payload.get("expires_in", 7199)
        try:
            expires = float(expires_in)
        except (TypeError, ValueError):
            expires = 7199.0
        log.debug("opsramp.token_acquired", expires_in=expires)
        return str(token), expires

    def _post_token(self, form: dict[str, str], *, json_body: bool = False) -> httpx.Response:
        kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
        if json_body:
            kwargs["json"] = form
        else:
            kwargs["data"] = form
            kwargs["headers"] = {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        try:
            return self._client.post(self.token_url, **kwargs)
        except httpx.HTTPError as exc:
            raise OpsRampAuthError(f"Unable to reach the OpsRamp token endpoint: {exc}") from exc


class OpsRampClient:
    """Thin, typed wrapper over the OpsRamp Cloud API."""

    def __init__(
        self,
        settings: OpsRampSettings,
        http_client: httpx.Client | None = None,
        token_manager: OpsRampTokenManager | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = http_client is None
        self._http = http_client or build_client(timeout=settings.timeout_s)
        self.policy = RetryPolicy(max_retries=settings.max_retries)
        self._tokens = token_manager or OpsRampTokenManager(
            self._http,
            base_url=settings.base_url,
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
            scope=settings.scope,
            refresh_margin_s=settings.token_refresh_margin_s,
            policy=self.policy,
        )

    @property
    def api_base(self) -> str:
        return f"{self.settings.base_url.rstrip('/')}/api"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.access_token()}",
            "Accept": "application/json",
        }

    def _refresh_auth(self) -> dict[str, str]:
        """Invalidate the cached token so the retried request carries a fresh one."""
        self._tokens.invalidate()
        return {
            "Authorization": f"Bearer {self._tokens.access_token()}",
            "Accept": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Perform an authenticated request and return the decoded JSON body."""
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        response = request_with_retry(
            self._http,
            method,
            url,
            policy=self.policy,
            headers={**self._auth_headers(), **(headers or {})},
            refresh_auth=self._refresh_auth,
            params=clean_params or None,
            json=json_body,
            data=data,
        )
        return self._handle(response)

    def _handle(self, response: httpx.Response) -> Any:
        status = response.status_code
        if status == 404:
            raise OpsRampNotFoundError(f"OpsRamp resource not found: {response.request.url}")
        if status == 410:
            raise OpsRampApiError(
                "OpsRamp returned 410 Gone - the tenant is deactivated or the license expired"
            )
        if status == 429:
            raise OpsRampRateLimitError(
                f"OpsRamp rate limit exceeded for {response.request.url}; "
                "reduce poll frequency or narrow the client filter"
            )
        if status in (401, 403, 407):
            raise OpsRampAuthError(
                f"OpsRamp rejected the request (HTTP {status}): {_body_snippet(response)}"
            )
        if status >= 400:
            raise OpsRampApiError(
                f"OpsRamp API error (HTTP {status}) for {response.request.url}: "
                f"{_body_snippet(response)}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a paged ``/search`` endpoint.

        The OpsRamp search endpoints return ``{"results": [...], "nextPage": bool}``
        and are advanced with ``pageNo``.
        """
        page_no = 1
        while page_no <= MAX_PAGES:
            page_params = dict(params or {})
            page_params.update({"pageNo": page_no, "pageSize": page_size})
            payload = self.get(path, params=page_params)
            if not isinstance(payload, dict):
                return
            results = payload.get("results") or []
            yield from results
            if not payload.get("nextPage"):
                return
            page_no += 1

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> OpsRampClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _body_snippet(response: httpx.Response, limit: int = 300) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - defensive: never fail on an unreadable body
        return "<unreadable body>"
    text = " ".join(text.split())
    return text[:limit]
