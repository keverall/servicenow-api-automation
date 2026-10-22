"""Configuration loading: environment variables, list parsing and YAML overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from opsramp_automation.config import AppSettings, load_settings
from opsramp_automation.errors import ConfigError


def test_the_defaults_are_safe() -> None:
    settings = AppSettings()

    assert settings.sync.dry_run is True
    assert settings.sync.states == ["Critical", "Warning", "Ok"]
    assert settings.sync.max_attempts == 3
    assert settings.deploy.provisioner == "existing"


def test_credentials_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSRAMP_TENANT_ID", "client_1")
    monkeypatch.setenv("OPSRAMP_CLIENT_ID", "key")
    monkeypatch.setenv("OPSRAMP_CLIENT_SECRET", "secret")

    settings = load_settings()

    assert settings.opsramp.tenant_id == "client_1"
    assert settings.opsramp.client_id == "key"
    assert settings.opsramp.client_secret.get_secret_value() == "secret"


def test_the_api_key_aliases_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSRAMP_API_KEY", "key")
    monkeypatch.setenv("OPSRAMP_API_SECRET", "secret")

    settings = load_settings()

    assert settings.opsramp.client_id == "key"
    assert settings.opsramp.client_secret.get_secret_value() == "secret"


def test_incomplete_credentials_are_reported() -> None:
    settings = AppSettings()
    settings.opsramp.tenant_id = ""

    with pytest.raises(ConfigError):
        settings.opsramp.require_credentials()


def test_comma_separated_lists_are_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLIENT_IDS", "client_1, client_2")
    monkeypatch.setenv("SYNC_STATES", "Critical,Warning")

    settings = load_settings()

    assert settings.sync.client_ids == ["client_1", "client_2"]
    assert settings.sync.states == ["Critical", "Warning"]


def test_json_arrays_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOY_NTP_SERVERS", '["1.1.1.1", "2.2.2.2"]')

    settings = load_settings()

    assert settings.deploy.ntp_servers == ["1.1.1.1", "2.2.2.2"]


def test_a_structured_list_item_is_rejected_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOY_SYSLOG_RECEIVERS", '[{"receiver": "10.0.0.5"}]')

    with pytest.raises(ValidationError):
        load_settings()


def test_a_yaml_file_overrides_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_POLL_INTERVAL_S", "300")
    config = tmp_path / "config.yaml"
    config.write_text("sync:\n  poll_interval_s: 60\n", encoding="utf-8")

    settings = load_settings(config)

    assert settings.sync.poll_interval_s == 60


def test_a_missing_yaml_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_the_servicenow_base_url_is_derived_from_the_instance() -> None:
    settings = AppSettings()
    settings.servicenow.instance = "dev12345"

    assert settings.servicenow.base_url == "https://dev12345.service-now.com"
