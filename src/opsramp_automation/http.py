"""Shared HTTP plumbing: client construction, retries, rate limits and auth refresh.

Both the OpsRamp cloud client and the ServiceNow Table API client are built on
top of :func:`request_with_retry` so that retry, backoff and rate-limit
semantics are defined exactly once.
"""

from __future__ import annotations

import random
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_MAX_BACKOFF_S = 30.0
DEFAULT_RATE_LIMIT_CAP_S = 60.0
DEFAULT_MAX_JITTER_S = 1.0

#: Transient server-side statuses worth retrying.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: Statuses that mean "your credentials/token went stale" rather than "bad request".
AUTH_RETRY_STATUS: frozenset[int] = frozenset({401, 407})


class RetryableStatusError(Exception):
    """Internal marker carrying the response that should be retried."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"retryable HTTP status {response.status_code}")


@dataclass(frozen=True)
class RetryPolicy:
    """Tunables for the shared retry loop."""

    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay: float = 1.0
    max_delay: float = DEFAULT_MAX_BACKOFF_S
    rate_limit_cap: float = DEFAULT_RATE_LIMIT_CAP_S
    max_jitter: float = DEFAULT_MAX_JITTER_S

    @property
    def max_attempts(self) -> int:
        return max(1, self.max_retries + 1)


def build_client(
    verify: bool | str | ssl.SSLContext = True,
    timeout: float = DEFAULT_TIMEOUT_S,
    *,
    ca_bundle: str | None = None,
    transport: httpx.BaseTransport | None = None,
    http2: bool = False,
) -> httpx.Client:
    """Create the shared :class:`httpx.Client`.

    ``ca_bundle`` points at a PEM file holding the (self-signed) gateway CA;
    when supplied it takes precedence over ``verify``.  Pass an
    :class:`ssl.SSLContext` to ``verify`` to control the trust store directly.
    """
    verify_option: bool | str | ssl.SSLContext = ca_bundle if ca_bundle else verify
    return httpx.Client(
        verify=verify_option,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        transport=transport,
        http2=http2,
        follow_redirects=True,
    )


def rate_limit_delay(
    response: httpx.Response, cap: float = DEFAULT_RATE_LIMIT_CAP_S
) -> float | None:
    """Return the number of seconds to wait for a 429, or ``None`` when unknown.

    OpsRamp returns ``x-ratelimit-reset`` as epoch seconds.
    """
    raw = response.headers.get("x-ratelimit-reset")
    if not raw:
        return None
    try:
        reset_at = float(raw)
    except (TypeError, ValueError):
        return None
    if reset_at <= 0:
        return None
    return min(max(0.0, reset_at - time.time()), cap)


def _compute_delay(attempt: int, response: httpx.Response | None, policy: RetryPolicy) -> float:
    if response is not None and response.status_code == 429:
        delay = rate_limit_delay(response, policy.rate_limit_cap)
        if delay is not None:
            return delay
    exponential: float = min(policy.base_delay * (2 ** max(0, attempt - 1)), policy.max_delay)
    jitter: float = random.uniform(0, policy.max_jitter)  # noqa: S311 - jitter, not crypto
    return exponential + jitter


class _ResponseAwareWait:
    """Tenacity wait strategy that honours ``x-ratelimit-reset``."""

    def __init__(self, policy: RetryPolicy) -> None:
        self.policy = policy

    def __call__(self, retry_state: RetryCallState) -> float:
        attempt = max(1, retry_state.attempt_number)
        response: httpx.Response | None = None
        outcome = retry_state.outcome
        if outcome is not None and outcome.failed:
            exc = outcome.exception()
            if isinstance(exc, RetryableStatusError):
                response = exc.response
        return _compute_delay(attempt, response, self.policy)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    policy: RetryPolicy | None = None,
    headers: Mapping[str, str] | None = None,
    refresh_auth: Callable[[], Mapping[str, str] | None] | None = None,
    retryable_status: frozenset[int] = RETRYABLE_STATUS,
    **kwargs: Any,
) -> httpx.Response:
    """Perform a request with retries, rate-limit handling and one auth refresh.

    ``refresh_auth`` is invoked when the response status is 401 or 407 (an
    expired token).  It must return replacement headers -- typically a fresh
    ``Authorization`` header -- or ``None`` to give up.  The request is then
    replayed exactly once.
    """
    active_policy = policy or RetryPolicy()
    request_headers = dict(headers or {})

    def attempt() -> httpx.Response:
        response = client.request(method, url, headers=request_headers, **kwargs)
        if response.status_code in retryable_status:
            raise RetryableStatusError(response)
        return response

    retrying = Retrying(
        stop=stop_after_attempt(active_policy.max_attempts),
        wait=_ResponseAwareWait(active_policy),
        retry=retry_if_exception_type(RetryableStatusError),
        reraise=True,
    )
    try:
        response = retrying(attempt)
    except RetryableStatusError as exc:
        # The retry budget is exhausted: hand the response back so that the
        # caller can turn it into a domain specific error.
        return exc.response

    if response.status_code in AUTH_RETRY_STATUS and refresh_auth is not None:
        replacement = refresh_auth()
        if replacement:
            request_headers.update(replacement)
            return client.request(method, url, headers=request_headers, **kwargs)
    return response
