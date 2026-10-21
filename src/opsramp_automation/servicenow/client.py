"""ServiceNow Table API client (basic or OAuth auth)."""

from __future__ import annotations

import base64
import threading
import time
from typing import Any

import httpx

from ..config import ServiceNowSettings
from ..errors import ServiceNowAuthError, ServiceNowError
from ..http import RetryPolicy, build_client, request_with_retry
from ..logging import get_logger

log = get_logger(__name__)


def escape_query_value(value: str) -> str:
    """Escape a value for use inside a ServiceNow encoded query.

    ``^`` is the operator separator and ``=`` starts a new condition, so both
    have to be escaped in a literal value.
    """
    return value.replace("^", "^^").replace("=", "^=")


class ServiceNowNotFoundError(ServiceNowError):
    """The requested ServiceNow record does not exist."""


class ServiceNowClient:
    """Minimal client for the ServiceNow Table API.

    Only the operations the sync engine needs are implemented: create, patch,
    query and a CMDB CI lookup.
    """

    def __init__(
        self,
        settings: ServiceNowSettings,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = http_client is None
        self._http = http_client or build_client(timeout=settings.timeout_s)
        self.policy = RetryPolicy(max_retries=settings.max_retries)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._last_request_at: float = 0.0

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    def _basic_headers(self) -> dict[str, str]:
        raw = f"{self.settings.username}:{self.settings.password.get_secret_value()}".encode()
        return {
            "Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}",
            "Accept": "application/json",
        }

    def _oauth_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            response = self._http.post(
                f"{self.settings.base_url}/api/now/oauth/token.do",
                data={
                    "grant_type": "password",
                    "username": self.settings.username,
                    "password": self.settings.password.get_secret_value(),
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret.get_secret_value(),
                },
                headers={"Accept": "application/json"},
            )
            if response.status_code in (400, 401, 403):
                raise ServiceNowAuthError("ServiceNow rejected the OAuth credentials")
            if response.status_code >= 400:
                raise ServiceNowError(
                    f"ServiceNow token request failed (HTTP {response.status_code})"
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise ServiceNowError("ServiceNow token response did not contain an access token")
            try:
                expires_in = float(payload.get("expires_in", 1800))
            except (TypeError, ValueError):
                expires_in = 1800.0
            self._token = str(token)
            self._token_expires_at = time.time() + max(0.0, expires_in - 30)
            return self._token

    def _headers(self) -> dict[str, str]:
        if self.settings.auth_mode == "oauth":
            return {
                "Authorization": f"Bearer {self._oauth_token()}",
                "Accept": "application/json",
            }
        return self._basic_headers()

    # ------------------------------------------------------------------
    # request plumbing
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        interval = self.settings.min_interval_s
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_at = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self._throttle()
        url = f"{self.settings.base_url}{path}"
        try:
            response = request_with_retry(
                self._http,
                method,
                url,
                policy=self.policy,
                headers=self._headers(),
                json=json_body,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise ServiceNowError(
                f"Cannot reach ServiceNow at {self.settings.base_url}: {exc}"
            ) from exc
        if response.status_code in (401, 403):
            raise ServiceNowAuthError(
                f"ServiceNow rejected the credentials (HTTP {response.status_code})"
            )
        if response.status_code == 404:
            raise ServiceNowNotFoundError(f"ServiceNow record not found: {path}")
        if response.status_code >= 400:
            raise ServiceNowError(
                f"ServiceNow returned HTTP {response.status_code} for {path}: {_snippet(response)}"
            )
        return response

    # ------------------------------------------------------------------
    # table operations
    # ------------------------------------------------------------------
    def create(self, table: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Insert a record and return the created record."""
        response = self._request("POST", f"/api/now/table/{table}", json_body=payload)
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise ServiceNowError(f"ServiceNow did not return a created record for {table}")
        return result

    def update(self, table: str, sys_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Patch an existing record and return the updated record."""
        response = self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json_body=payload)
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise ServiceNowError(
                f"ServiceNow did not return an updated record for {table}/{sys_id}"
            )
        return result

    def query(
        self,
        table: str,
        query: str,
        *,
        fields: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run an encoded query and return the matching records."""
        params: dict[str, Any] = {"sysparm_query": query}
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        params["sysparm_limit"] = limit or self.settings.sysparm_limit
        response = self._request("GET", f"/api/now/table/{table}", params=params)
        body = response.json()
        if not isinstance(body, dict):
            return []
        result = body.get("result")
        return list(result) if isinstance(result, list) else []

    def find_by_correlation(
        self,
        table: str,
        correlation_id: str,
        *,
        dedup_field: str = "correlation_id",
        fields: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Return the first record matching the dedup key, or ``None``.

        ``fields`` controls which columns are returned; by default only the
        identity columns are fetched.
        """
        requested = list(fields) if fields else ["sys_id", "number", "state"]
        if dedup_field not in requested:
            requested.append(dedup_field)
        records = self.query(
            table,
            f"{dedup_field}={escape_query_value(correlation_id)}",
            fields=requested,
            limit=1,
        )
        return records[0] if records else None

    def lookup_ci(self, hostname: str) -> dict[str, Any] | None:
        """Look up a CMDB CI by name or host name."""
        if not hostname:
            return None
        records = self.query(
            "cmdb_ci",
            f"name={escape_query_value(hostname)}^ORsys_host_name={escape_query_value(hostname)}",
            fields=["sys_id", "name"],
            limit=1,
        )
        return records[0] if records else None

    def get_record(
        self, table: str, sys_id: str, fields: list[str] | None = None
    ) -> dict[str, Any]:
        """Fetch a single record by sys_id."""
        params: dict[str, Any] = {}
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        response = self._request("GET", f"/api/now/table/{table}/{sys_id}", params=params)
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise ServiceNowNotFoundError(f"ServiceNow record {table}/{sys_id} not found")
        return result

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> ServiceNowClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _snippet(response: httpx.Response, limit: int = 400) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - defensive: never fail on an unreadable body
        return "<unreadable body>"
    return " ".join(text.split())[:limit]
