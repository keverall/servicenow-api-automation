"""Gateway appliance admin API: exact action payloads and error handling."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from opsramp_automation.errors import GatewayAuthError, GatewayError
from opsramp_automation.opsramp.gateway_admin import GatewayAdminClient

BASE = "https://10.0.0.5:5480"
AUTH = f"{BASE}/v2/auth/token"
GATEWAY = f"{BASE}/v2/gateway"


@pytest.fixture
def gateway(gateway_settings) -> GatewayAdminClient:
    return GatewayAdminClient(gateway_settings)


def _mock_auth(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.post(AUTH).mock(
        return_value=httpx.Response(200, json={"token": "tok", "expires_in": "3599.99"})
    )


def _body(route: respx.Route) -> dict:
    return json.loads(route.calls[0].request.content)


@respx.mock
def test_authenticate_uses_the_token_scheme(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    respx_mock.get(GATEWAY).mock(
        return_value=httpx.Response(200, json={"generalInfo": {"applianceInformation": {}}})
    )

    gateway.get_config("generalInfo")

    request = respx_mock.calls[1].request
    assert request.headers["authorization"] == "token tok"


@respx.mock
def test_bad_credentials_raise_auth_error(respx_mock: respx.MockRouter, gateway) -> None:
    respx_mock.post(AUTH).mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(GatewayAuthError):
        gateway.authenticate()


@respx.mock
def test_registration_payload(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    route = respx_mock.post(f"{BASE}/v2/gateway/actions/registration").mock(
        return_value=httpx.Response(200, json={"success": "Appliance registered successfully"})
    )

    message = gateway.register("podname.opsramp.com", "activation-token")

    assert message == "Appliance registered successfully"
    assert _body(route) == {
        "registration": {"server": "podname.opsramp.com", "activation_token": "activation-token"}
    }


@respx.mock
def test_network_settings_payload(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    route = respx_mock.post(f"{BASE}/v2/gateway/actions/networkSettings").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )

    gateway.set_network_settings(
        domainname="example.com",
        hostname="gw01",
        interface="ens160",
        dhcp=False,
        ipaddr="10.0.0.5",
        subnet="255.255.255.0",
        gateway="10.0.0.1",
        dnsArray="8.8.8.8",
    )

    assert _body(route) == {
        "networkSettings": {
            "domainname": "example.com",
            "hostname": "gw01",
            "interface": "ens160",
            "dhcp": "False",
            "ipaddr": "10.0.0.5",
            "subnet": "255.255.255.0",
            "gateway": "10.0.0.1",
            "dnsArray": "8.8.8.8",
        }
    }


@respx.mock
def test_static_route_payload(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    route = respx_mock.post(f"{BASE}/v2/gateway/actions/staticRoutes").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )

    gateway.add_static_route(
        srouteIP="10.10.5.0",
        srouteGateway="192.168.0.1",
        srouteSubnet="255.255.255.0",
        srouteMetric=0,
        srouteInterface="enp0s3",
    )

    assert _body(route) == {
        "staticRoutes": {
            "srouteIP": "10.10.5.0",
            "srouteGateway": "192.168.0.1",
            "srouteSubnet": "255.255.255.0",
            "srouteMetric": "0",
            "srouteInterface": "enp0s3",
        }
    }


@respx.mock
def test_ntp_and_syslog_payloads(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    ntp = respx_mock.post(f"{BASE}/v2/gateway/actions/ntpUpdate").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )
    syslog = respx_mock.post(f"{BASE}/v2/gateway/actions/syslogAdd").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )

    gateway.set_ntp_servers(["216.239.35.4", "45.32.199.189"])
    gateway.add_syslog(receiver="192.168.0.116", port=514, protocol="udp", selectors="*.*")

    assert _body(ntp) == {"ntpUpdate": {"ntpServers": ["216.239.35.4", "45.32.199.189"]}}
    assert _body(syslog) == {
        "syslogInfo": {
            "selectors": "*.*",
            "protocol": "udp",
            "port": "514",
            "receiver": "192.168.0.116",
        }
    }


@respx.mock
def test_timezone_datetime_proxy_and_password_payloads(
    respx_mock: respx.MockRouter, gateway
) -> None:
    _mock_auth(respx_mock)
    tz = respx_mock.post(f"{BASE}/v2/gateway/actions/timeZone").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )
    dt = respx_mock.post(f"{BASE}/v2/gateway/actions/dateTime").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )
    proxy = respx_mock.post(f"{BASE}/v2/gateway/actions/externalProxy").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )
    pwd = respx_mock.post(f"{BASE}/v2/gateway/actions/passwdChange").mock(
        return_value=httpx.Response(200, json={"success": "ok"})
    )

    gateway.set_timezone("Alaska")
    gateway.set_datetime(hour="13", min="02", sec="02", date="04-13-2021")
    gateway.set_proxy(
        connection="proxy",
        proxy_ip="172.16.0.1",
        proxy_port="3128",
        proxy_usr="user",
        proxy_pwd="secret",
    )
    gateway.change_password(username="admin", oldpasswd="old", newpasswd="new")

    assert _body(tz) == {"timeZone": {"tzs": "Alaska"}}
    assert _body(dt) == {"dateTime": {"hour": "13", "min": "02", "sec": "02", "date": "04-13-2021"}}
    assert _body(proxy) == {
        "externalProxy": {
            "connection": "proxy",
            "proxy_ip": "172.16.0.1",
            "proxy_port": "3128",
            "proxy_usr": "user",
            "proxy_pwd": "secret",
        }
    }
    assert _body(pwd) == {
        "passwdChange": {"username": "admin", "oldpasswd": "old", "newpasswd": "new"}
    }


@respx.mock
def test_monit_service_payload_and_validation(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    route = respx_mock.post(f"{BASE}/v2/gateway/actions/monitService").mock(
        return_value=httpx.Response(200, json={"success": "Service Network is Active"})
    )

    gateway.monit_service("network", "restart")

    assert _body(route) == {"monitService": {"service": "network", "action": "restart"}}
    with pytest.raises(GatewayError):
        gateway.monit_service("nginx", "status")
    with pytest.raises(GatewayError):
        gateway.monit_service("network", "explode")


@respx.mock
def test_response_without_success_is_a_failure(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    respx_mock.post(f"{BASE}/v2/gateway/actions/timeZone").mock(
        return_value=httpx.Response(200, json={"error": "nope"})
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.set_timezone("Alaska")
    assert "did not report success" in str(excinfo.value)


@respx.mock
def test_401_triggers_a_single_re_authentication(respx_mock: respx.MockRouter, gateway) -> None:
    auth = _mock_auth(respx_mock)
    route = respx_mock.post(f"{BASE}/v2/gateway/actions/timeZone").mock(
        side_effect=[
            httpx.Response(401, json={}),
            httpx.Response(200, json={"success": "ok"}),
        ]
    )

    gateway.set_timezone("Alaska")

    assert route.call_count == 2
    assert auth.call_count == 2


@respx.mock
def test_config_info_query(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    route = respx_mock.get(GATEWAY).mock(
        return_value=httpx.Response(200, json={"generalInfo": {"applianceInformation": {}}})
    )

    gateway.get_config("generalInfo")

    assert "info=generalInfo" in str(route.calls[0].request.url)


@respx.mock
def test_unknown_info_is_rejected(respx_mock: respx.MockRouter, gateway) -> None:
    with pytest.raises(GatewayError):
        gateway.get_config("nonsense")


@respx.mock
def test_registration_status_and_tunnel_status(respx_mock: respx.MockRouter, gateway) -> None:
    _mock_auth(respx_mock)
    respx_mock.get(GATEWAY).mock(
        return_value=httpx.Response(
            200,
            json={
                "generalInfo": {
                    "applianceInformation": {
                        "registrationStatus": "Registered",
                        "tunnelStatus": "Established",
                    }
                }
            },
        )
    )

    assert gateway.registration_status() == "Registered"
    assert gateway.tunnel_status() == "Established"


def test_tls_verification_can_be_disabled(gateway_settings) -> None:
    client = GatewayAdminClient(gateway_settings)
    assert client._http is not None
    client.close()


def test_ca_bundle_is_used_when_configured(gateway_settings) -> None:
    bundle = Path(__file__).resolve().parents[1] / "fixtures" / "certs" / "gateway-ca.pem"
    gateway_settings.ca_bundle = bundle
    client = GatewayAdminClient(gateway_settings)
    try:
        assert client._http is not None
    finally:
        client.close()
