"""OpsRamp cloud client: token handling, retries, pagination and error mapping."""

from __future__ import annotations

import httpx
import pytest
import respx

from opsramp_automation.errors import (
    OpsRampApiError,
    OpsRampAuthError,
    OpsRampNotFoundError,
    OpsRampRateLimitError,
)
from opsramp_automation.opsramp.client import OpsRampClient, OpsRampTokenManager

TOKEN_URL = "https://api.opsramp.com/tenancy/auth/oauth/token"
API = "https://api.opsramp.com/api"


@respx.mock
def test_token_is_fetched_once_and_reused(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{API}/v2/tenants/client_22568/thing").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    with OpsRampClient(opsramp_settings) as client:
        client.get("/v2/tenants/client_22568/thing")
        client.get("/v2/tenants/client_22568/thing")

    assert token.call_count == 1
    request = token.calls[0].request
    assert "application/x-www-form-urlencoded" in request.headers["content-type"]
    assert b"grant_type=client_credentials" in request.content
    assert b"scope=global%3Amanage" in request.content


@respx.mock
def test_token_manager_refreshes_when_expired(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 1})
    )
    respx_mock.get(f"{API}/v2/ping").mock(return_value=httpx.Response(200, json={}))

    with OpsRampClient(opsramp_settings) as client:
        client.get("/v2/ping")
        # The token has a one second lifetime and a 60 second refresh margin,
        # so the next call must fetch a new one.
        client.get("/v2/ping")

    assert token.call_count == 2


@respx.mock
def test_token_falls_back_to_json_body(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, json={"code": "0005", "message": "bad request"}),
            httpx.Response(200, json={"access_token": "abc", "expires_in": 7199}),
        ]
    )
    respx_mock.get(f"{API}/v2/ping").mock(return_value=httpx.Response(200, json={}))

    with OpsRampClient(opsramp_settings) as client:
        client.get("/v2/ping")

    assert token.call_count == 2
    assert b'"grant_type"' in token.calls[1].request.content


@respx.mock
def test_bad_credentials_raise_auth_error(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )
    with OpsRampClient(opsramp_settings) as client, pytest.raises(OpsRampAuthError):
        client.get("/v2/ping")


@respx.mock
def test_401_triggers_a_single_token_refresh(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    route = respx_mock.get(f"{API}/v2/ping").mock(
        side_effect=[
            httpx.Response(401, json={"message": "token expired"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    with OpsRampClient(opsramp_settings) as client:
        assert client.get("/v2/ping") == {"ok": True}

    assert route.call_count == 2
    assert token.call_count == 2
    assert route.calls[1].request.headers["authorization"] == "Bearer abc"


@respx.mock
def test_429_honours_the_ratelimit_reset_header(
    respx_mock: respx.MockRouter, opsramp_settings
) -> None:
    import time

    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{API}/v2/ping").mock(
        side_effect=[
            httpx.Response(
                429, json={"message": "slow down"}, headers={"x-ratelimit-reset": str(int(time.time()))}
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    with OpsRampClient(opsramp_settings) as client:
        assert client.get("/v2/ping") == {"ok": True}


@respx.mock
def test_429_that_never_clears_raises_rate_limit_error(
    respx_mock: respx.MockRouter, opsramp_settings
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{API}/v2/ping").mock(
        return_value=httpx.Response(429, json={"message": "slow down"})
    )

    with OpsRampClient(opsramp_settings) as client, pytest.raises(OpsRampRateLimitError):
        client.get("/v2/ping")


@respx.mock
def test_404_raises_not_found(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{API}/v2/missing").mock(return_value=httpx.Response(404, json={}))

    with OpsRampClient(opsramp_settings) as client, pytest.raises(OpsRampNotFoundError):
        client.get("/v2/missing")


@respx.mock
def test_410_fails_hard(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{API}/v2/gone").mock(return_value=httpx.Response(410, json={}))

    with OpsRampClient(opsramp_settings) as client, pytest.raises(OpsRampApiError) as excinfo:
        client.get("/v2/gone")
    assert "tenant is deactivated" in str(excinfo.value)


@respx.mock
def test_paginate_follows_next_page(respx_mock: respx.MockRouter, opsramp_settings) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    search = respx_mock.get(f"{API}/v2/tenants/client_22568/alerts/search").mock(
        side_effect=[
            httpx.Response(
                200, json={"results": [{"uniqueId": "1"}], "nextPage": True, "pageNo": 1}
            ),
            httpx.Response(
                200, json={"results": [{"uniqueId": "2"}], "nextPage": False, "pageNo": 2}
            ),
        ]
    )

    with OpsRampClient(opsramp_settings) as client:
        items = list(client.paginate("/v2/tenants/client_22568/alerts/search", page_size=1))

    assert [item["uniqueId"] for item in items] == ["1", "2"]
    assert search.call_count == 2
    assert "pageNo=2" in str(search.calls[1].request.url)


def test_token_manager_is_thread_safe(opsramp_settings) -> None:
    import threading

    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"access_token": "abc", "expires_in": 7199}

    class FakeClient:
        def post(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append(url)
            return FakeResponse()

    manager = OpsRampTokenManager(
        FakeClient(),  # type: ignore[arg-type]
        base_url="https://api.opsramp.com",
        client_id="key",
        client_secret="secret",
    )
    threads = [threading.Thread(target=manager.access_token) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
