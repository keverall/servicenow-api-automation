"""ServiceNow Table API client: query encoding, correlation lookup and errors."""

from __future__ import annotations

import httpx
import pytest
import respx

from opsramp_automation.errors import ServiceNowAuthError, ServiceNowError
from opsramp_automation.servicenow.client import ServiceNowClient, escape_query_value

BASE = "https://dev12345.service-now.com"
TABLE = f"{BASE}/api/now/table/event"


@pytest.fixture
def client(servicenow_settings) -> ServiceNowClient:
    return ServiceNowClient(servicenow_settings)


def test_query_values_are_escaped() -> None:
    assert escape_query_value("plain") == "plain"
    assert escape_query_value("a^b") == "a^^b"
    assert escape_query_value("a=b") == "a^=b"


@respx.mock
def test_basic_auth_header(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.get(TABLE).mock(return_value=httpx.Response(200, json={"result": []}))

    client.query("event", "correlation_id=abc")

    authorization = route.calls[0].request.headers["authorization"]
    assert authorization.startswith("Basic ")


@respx.mock
def test_query_encodes_the_sysparm_query(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.get(TABLE).mock(return_value=httpx.Response(200, json={"result": []}))

    client.query("event", "correlation_id=opsramp:client_1:42", fields=["sys_id", "number"])

    url = str(route.calls[0].request.url)
    assert "sysparm_query=correlation_id%3Dopsramp%3Aclient_1%3A42" in url
    assert "sysparm_fields=sys_id%2Cnumber" in url
    assert "sysparm_limit=100" in url


@respx.mock
def test_find_by_correlation_returns_the_first_match(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.get(TABLE).mock(
        return_value=httpx.Response(
            200, json={"result": [{"sys_id": "abc", "number": "EVT001", "state": "1"}]}
        )
    )

    record = client.find_by_correlation("event", "opsramp:client_1:42")

    assert record is not None
    assert record["sys_id"] == "abc"
    assert "sysparm_limit=1" in str(route.calls[0].request.url)


@respx.mock
def test_find_by_correlation_returns_none_when_absent(respx_mock: respx.MockRouter, client) -> None:
    respx_mock.get(TABLE).mock(return_value=httpx.Response(200, json={"result": []}))
    assert client.find_by_correlation("event", "missing") is None


@respx.mock
def test_create_posts_json_and_returns_the_result(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.post(TABLE).mock(
        return_value=httpx.Response(201, json={"result": {"sys_id": "abc", "number": "EVT001"}})
    )

    created = client.create("event", {"source": "OpsRamp"})

    assert created["sys_id"] == "abc"
    assert route.calls[0].request.headers["content-type"] == "application/json"


@respx.mock
def test_update_uses_patch(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.patch(f"{TABLE}/abc").mock(
        return_value=httpx.Response(200, json={"result": {"sys_id": "abc", "state": "6"}})
    )

    updated = client.update("event", "abc", {"state": "6"})

    assert updated["state"] == "6"
    assert route.calls[0].request.method == "PATCH"


@respx.mock
def test_lookup_ci_queries_both_name_and_host_name(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.get(f"{BASE}/api/now/table/cmdb_ci").mock(
        return_value=httpx.Response(200, json={"result": [{"sys_id": "ci1", "name": "host1"}]})
    )

    record = client.lookup_ci("host1")

    assert record is not None
    assert record["sys_id"] == "ci1"
    assert "sysparm_query=name%3Dhost1%5EORsys_host_name%3Dhost1" in str(route.calls[0].request.url)


@respx.mock
def test_401_fails_fast_without_retry(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.get(TABLE).mock(return_value=httpx.Response(401, json={}))

    with pytest.raises(ServiceNowAuthError):
        client.query("event", "correlation_id=abc")

    assert route.call_count == 1


@respx.mock
def test_429_is_retried(respx_mock: respx.MockRouter, client) -> None:
    route = respx_mock.get(TABLE).mock(
        side_effect=[
            httpx.Response(429, json={}),
            httpx.Response(200, json={"result": []}),
        ]
    )

    client.query("event", "correlation_id=abc")

    assert route.call_count == 2


@respx.mock
def test_500_after_the_retry_budget_raises(respx_mock: respx.MockRouter, client) -> None:
    respx_mock.get(TABLE).mock(return_value=httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(ServiceNowError):
        client.query("event", "correlation_id=abc")


@respx.mock
def test_oauth_mode_uses_a_bearer_token(respx_mock: respx.MockRouter, servicenow_settings) -> None:
    servicenow_settings.auth_mode = "oauth"
    servicenow_settings.client_id = "cid"
    servicenow_settings.client_secret = "csecret"
    respx_mock.post(f"{BASE}/api/now/oauth/token.do").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
    )
    route = respx_mock.get(TABLE).mock(return_value=httpx.Response(200, json={"result": []}))

    client = ServiceNowClient(servicenow_settings)
    client.query("event", "correlation_id=abc")

    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
