"""The command line interface: wiring, exit codes and output."""

from __future__ import annotations

import httpx
import respx
from typer.testing import CliRunner

from opsramp_automation.cli import app

runner = CliRunner()

TOKEN_URL = "https://api.opsramp.com/tenancy/auth/oauth/token"
GATEWAY = "https://10.0.0.5:5480"

GENERAL_INFO = {
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
            "registrationStatus": "Registered",
            "tunnelStatus": "Established",
            "applianceFirmwareVersion": "5.1.0",
            "model": "Classic",
        },
    }
}


def test_the_root_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("deploy", "sync", "doctor", "profile", "admin", "alerts", "integration"):
        assert command in result.stdout


def test_an_unknown_command_exits_with_a_usage_error() -> None:
    result = runner.invoke(app, ["not-a-command"])

    assert result.exit_code != 0


def test_doctor_reports_missing_configuration() -> None:
    result = runner.invoke(app, ["doctor"], env={"OPSRAMP_TENANT_ID": ""})

    assert result.exit_code == 2


@respx.mock
def test_admin_status_prints_the_appliance_state(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{GATEWAY}/v2/auth/token").mock(
        return_value=httpx.Response(200, json={"token": "gw-token", "expires_in": 3600})
    )
    respx_mock.get(f"{GATEWAY}/v2/gateway").mock(
        return_value=httpx.Response(200, json=GENERAL_INFO)
    )

    result = runner.invoke(
        app,
        ["admin", "status"],
        env={
            "GATEWAY_HOST": "10.0.0.5",
            "GATEWAY_PORT": "5480",
            "GATEWAY_USERNAME": "admin",
            "GATEWAY_PASSWORD": "Pass@1234",
            "GATEWAY_VERIFY_TLS": "false",
        },
    )

    assert result.exit_code == 0
    assert "Registered" in result.stdout


@respx.mock
def test_admin_config_rejects_an_unknown_section(respx_mock: respx.MockRouter) -> None:
    result = runner.invoke(
        app,
        ["admin", "config", "--info", "nonsense"],
        env={
            "GATEWAY_HOST": "10.0.0.5",
            "GATEWAY_USERNAME": "admin",
            "GATEWAY_PASSWORD": "Pass@1234",
            "GATEWAY_VERIFY_TLS": "false",
        },
    )

    assert result.exit_code == 5


def test_a_missing_opsramp_credential_exits_with_the_auth_code() -> None:
    result = runner.invoke(app, ["profile", "list"], env={"OPSRAMP_TENANT_ID": ""})

    assert result.exit_code == 2


def test_sync_rejects_a_mapping_file_that_does_not_exist(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["sync", "--once", "--dry-run", "--mapping", str(tmp_path / "nope.yaml")],
    )

    assert result.exit_code == 2
