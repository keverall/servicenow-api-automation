"""The deployment orchestrator: stage order, idempotency and dry runs."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from opsramp_automation.config import AppSettings, DeploySettings, GatewaySettings, OpsRampSettings
from opsramp_automation.deploy.orchestrator import DeploymentOrchestrator

TOKEN_URL = "https://api.opsramp.com/tenancy/auth/oauth/token"
API = "https://api.opsramp.com/api"
PROFILES = f"{API}/v2/tenants/client_22568/managementProfiles"
GATEWAY = "https://10.0.0.5:5480"
DOWNLOAD_URL = "https://dl01.opsramp.com/itop/download/gateway/abc"

GENERAL_INFO: dict[str, Any] = {
    "generalInfo": {
        "networkInformation": {
            "hostName": "gw-01",
            "ipAddress": "10.0.0.5",
            "subnetMask": "255.255.255.0",
            "defaultGateway": "10.0.0.1",
            "domainName": "example.com",
            "defaultInterface": "ens160",
            "dnsEntries": "10.0.0.1",
        },
        "applianceInformation": {
            "registrationStatus": "Unregistered",
            "tunnelStatus": "Disconnected",
            "applianceFirmwareVersion": "5.1.0",
        },
    }
}


@pytest.fixture
def settings(tmp_path) -> AppSettings:
    return AppSettings(
        opsramp=OpsRampSettings(
            base_url="https://api.opsramp.com",
            tenant_id="client_22568",
            client_id="key",
            client_secret="secret",
            max_retries=1,
        ),
        gateway=GatewaySettings(
            host="10.0.0.5",
            port=5480,
            username="admin",
            password="Pass@1234",
            verify_tls=False,
            registration_server="https://api.opsramp.com",
            max_retries=1,
        ),
        deploy=DeploySettings(
            profile_name="prod-gateway",
            package_name="ova",
            provisioner="existing",
            package_dir=tmp_path / "packages",
            verify_timeout_s=5,
            verify_interval_s=1,
        ),
    )


def mock_opsramp(respx_mock: respx.MockRouter, *, profile_exists: bool = False) -> None:
    """Register the OpsRamp cloud endpoints used by the deploy."""
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 7199})
    )
    results = (
        [{"id": 77, "name": "prod-gateway", "type": "Gateway", "registered": False}]
        if profile_exists
        else []
    )
    respx_mock.get(f"{PROFILES}/search").mock(
        return_value=httpx.Response(200, json={"results": results})
    )
    respx_mock.post(PROFILES).mock(
        return_value=httpx.Response(
            200, json={"id": 77, "name": "prod-gateway", "type": "Gateway", "registered": False}
        )
    )
    respx_mock.get(f"{PROFILES}/77/attach").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 77,
                "name": "prod-gateway",
                "activationToken": "token-xyz",
                "registered": False,
                "tunnelType": "edge",
            },
        )
    )
    respx_mock.get(f"{API}/v2/download/gateway/ova").mock(
        return_value=httpx.Response(200, json={"url": DOWNLOAD_URL, "expires_in": 900})
    )
    respx_mock.get(DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=b"fake-ova"))
    respx_mock.get(f"{PROFILES}/77").mock(
        return_value=httpx.Response(
            200, json={"id": 77, "name": "prod-gateway", "registered": True, "status": "UP"}
        )
    )


def mock_gateway(respx_mock: respx.MockRouter, *, registered: bool = False) -> None:
    """Register the appliance endpoints used by the deploy."""
    respx_mock.post(f"{GATEWAY}/v2/auth/token").mock(
        return_value=httpx.Response(200, json={"token": "gw-token", "expires_in": 3600})
    )
    general = {
        "generalInfo": {
            "networkInformation": GENERAL_INFO["generalInfo"]["networkInformation"],
            "applianceInformation": {
                **GENERAL_INFO["generalInfo"]["applianceInformation"],
                "registrationStatus": "Registered" if registered else "Unregistered",
                "tunnelStatus": "Established" if registered else "Disconnected",
            },
        }
    }
    respx_mock.get(f"{GATEWAY}/v2/gateway").mock(return_value=httpx.Response(200, json=general))
    respx_mock.post(f"{GATEWAY}/v2/gateway/actions/registration").mock(
        return_value=httpx.Response(200, json={"success": "Appliance registered successfully"})
    )


@respx.mock
def test_a_dry_run_changes_nothing(respx_mock: respx.MockRouter, settings) -> None:
    mock_opsramp(respx_mock, profile_exists=False)
    mock_gateway(respx_mock)

    report = DeploymentOrchestrator(settings, dry_run=True).run()

    assert report.dry_run is True
    assert report.succeeded is True
    stage_names = [stage.name for stage in report.stages]
    assert "profile" in stage_names
    assert "register" in stage_names
    assert all(stage.status != "failed" for stage in report.stages)
    # Nothing was written to the appliance.
    registration = respx_mock.get(f"{GATEWAY}/v2/gateway/actions/registration")
    assert not registration.called


@respx.mock
def test_a_full_deploy_registers_the_appliance(respx_mock: respx.MockRouter, settings) -> None:
    mock_opsramp(respx_mock, profile_exists=False)
    mock_gateway(respx_mock, registered=False)

    report = DeploymentOrchestrator(settings, dry_run=False).run()

    assert report.succeeded is True
    assert report.profile_id == 77
    assert report.activation_token == "token-xyz"
    assert report.registered is True
    names = [stage.name for stage in report.stages]
    assert names == [
        "profile",
        "attach",
        "package",
        "provision",
        "auth",
        "configure",
        "register",
        "verify",
    ]


@respx.mock
def test_an_already_registered_appliance_is_not_registered_again(
    respx_mock: respx.MockRouter, settings
) -> None:
    mock_opsramp(respx_mock, profile_exists=True)
    mock_gateway(respx_mock, registered=True)

    report = DeploymentOrchestrator(settings, dry_run=False).run()

    register = next(stage for stage in report.stages if stage.name == "register")
    assert register.status == "skipped"
    assert report.registered is True


@respx.mock
def test_an_existing_profile_is_reused(respx_mock: respx.MockRouter, settings) -> None:
    mock_opsramp(respx_mock, profile_exists=True)
    mock_gateway(respx_mock, registered=True)

    report = DeploymentOrchestrator(settings, dry_run=False).run()

    profile = next(stage for stage in report.stages if stage.name == "profile")
    assert profile.status == "skipped"


@respx.mock
def test_a_missing_profile_name_is_reported(respx_mock: respx.MockRouter, settings) -> None:
    settings.deploy.profile_name = ""

    report = DeploymentOrchestrator(settings, dry_run=True).run()

    assert report.succeeded is False
    assert any(stage.status == "failed" for stage in report.stages)
