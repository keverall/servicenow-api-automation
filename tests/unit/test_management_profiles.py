"""Management profile CRUD, attach/detach and package download."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import respx

from opsramp_automation.errors import OpsRampError
from opsramp_automation.opsramp.client import OpsRampClient
from opsramp_automation.opsramp.management_profiles import ManagementProfilesApi

TOKEN_URL = "https://api.opsramp.com/tenancy/auth/oauth/token"
API = "https://api.opsramp.com/api"
PROFILES = f"{API}/v2/tenants/client_22568/managementProfiles"


@pytest.fixture
def api(opsramp_settings) -> ManagementProfilesApi:
    client = OpsRampClient(opsramp_settings)
    return ManagementProfilesApi(client)


@respx.mock
def test_search_uses_the_query_string(respx_mock: respx.MockRouter, api) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    route = respx_mock.get(f"{PROFILES}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 77, "name": "Gateway Profile", "type": "Gateway", "registered": False}
                ]
            },
        )
    )

    profiles = api.list_profiles("name:Gateway_Profile")

    assert len(profiles) == 1
    assert profiles[0].name == "Gateway Profile"
    assert "queryString=name%3AGateway_Profile" in str(route.calls[0].request.url)


@respx.mock
def test_find_by_name_matches_exactly(respx_mock: respx.MockRouter, api) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{PROFILES}/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 1, "name": "Other"}, {"id": 2, "name": "Target"}]},
        )
    )

    assert api.find_by_name("Target").id == 2
    assert api.find_by_name("Missing") is None


@respx.mock
def test_create_profile_returns_the_activation_token(respx_mock: respx.MockRouter, api) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    route = respx_mock.post(PROFILES).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 77,
                "name": "Gateway Profile",
                "type": "Gateway",
                "registered": False,
                "activationToken": "6d8e4cc4-0be4-4706-bc1f-7e7e37a10b54",
            },
        )
    )

    profile = api.create_profile("Gateway Profile", "desc", "Gateway")

    assert profile.activationToken == "6d8e4cc4-0be4-4706-bc1f-7e7e37a10b54"
    assert json.loads(route.calls[0].request.content) == {
        "name": "Gateway Profile",
        "description": "desc",
        "type": "Gateway",
    }


@respx.mock
def test_attach_and_detach(respx_mock: respx.MockRouter, api) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{PROFILES}/77/attach").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 76,
                "name": "LB1 Gateway Profile",
                "type": "Gateway",
                "tunnelType": "SSHD",
                "activationToken": "d72eb58e-ff20-44e4-887c-341b0760eb00",
                "registered": False,
            },
        )
    )
    respx_mock.get(f"{PROFILES}/77/detach").mock(
        return_value=httpx.Response(200, json={"id": 76, "registered": False})
    )

    attached = api.attach(77)
    assert attached.activationToken == "d72eb58e-ff20-44e4-887c-341b0760eb00"
    assert api.detach(77)["id"] == 76


@respx.mock
def test_set_services_uses_the_documented_body(respx_mock: respx.MockRouter, api) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    route = respx_mock.post(f"{PROFILES}/77").mock(
        return_value=httpx.Response(200, json={"id": 77, "name": "p"})
    )

    api.set_services(77, {"sipAgent": True, "syntheticsAgent": False}, name="p")

    body = json.loads(route.calls[0].request.content)
    assert body["services"] == [
        {"name": "sipAgent", "status": "true"},
        {"name": "syntheticsAgent", "status": "false"},
    ]


@respx.mock
def test_download_url_and_package_download(respx_mock: respx.MockRouter, api, tmp_path) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    respx_mock.get(f"{API}/v2/download/gateway/ova").mock(
        return_value=httpx.Response(
            200,
            json={"url": "https://dl01.opsramp.com/itop/download/gateway/abc", "expires_in": 900},
        )
    )
    payload = b"fake-ova-bytes"
    respx_mock.get("https://dl01.opsramp.com/itop/download/gateway/abc").mock(
        return_value=httpx.Response(200, content=payload)
    )

    descriptor = api.get_download_url("ova")
    assert descriptor.expires_in == 900

    path = api.download_package("ova", tmp_path)
    assert path.read_bytes() == payload
    assert hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_unknown_package_is_rejected(api) -> None:
    with pytest.raises(OpsRampError):
        api.get_download_url("debian")


@respx.mock
def test_out_of_sync_search(respx_mock: respx.MockRouter, api) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    route = respx_mock.get(f"{PROFILES}/search").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1, "name": "p"}]})
    )

    api.find_out_of_sync("302443bc-8367-48c1-801b-2732d91c6d45")

    assert "outOfSync" in str(route.calls[0].request.url)
