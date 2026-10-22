"""Mapping OpsRamp alerts onto ServiceNow payloads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opsramp_automation.errors import ConfigError
from opsramp_automation.opsramp.models import Alert
from opsramp_automation.sync.mapper import (
    FieldMapping,
    correlation_id_for,
    is_problem_state,
    is_recovery,
    map_alert,
    normalise_state,
    render_description,
)


@pytest.fixture
def critical_alert(alert_payloads) -> Alert:
    return Alert.model_validate(alert_payloads[0])


@pytest.fixture
def recovery_alert(alert_payloads) -> Alert:
    return Alert.model_validate(alert_payloads[2])


def test_normalise_state_returns_the_canonical_spelling() -> None:
    assert normalise_state("critical") == "Critical"
    assert normalise_state("OK") == "Ok"
    assert normalise_state("something else") == "something else"


def test_problem_states_are_recognised() -> None:
    assert is_problem_state("Critical") is True
    assert is_problem_state("Warning") is True
    assert is_problem_state("Ok") is False
    assert is_problem_state(None) is False


def test_recovery_detection(critical_alert: Alert, recovery_alert: Alert) -> None:
    assert is_recovery(critical_alert) is False
    assert is_recovery(recovery_alert) is True


def test_correlation_id_is_stable_and_deterministic(critical_alert: Alert) -> None:
    mapping = FieldMapping.load(Path("config/mapping/event.yaml"))
    first = correlation_id_for(critical_alert, mapping)
    second = correlation_id_for(critical_alert, mapping)
    assert first == second
    assert first == "opsramp:client_22568:122620538"


def test_event_mapping_produces_the_expected_payload(critical_alert: Alert) -> None:
    mapping = FieldMapping.load(Path("config/mapping/event.yaml"))
    payload = map_alert(critical_alert, mapping)

    assert payload["source"] == "OpsRamp"
    assert payload["category"] == "Software"
    assert payload["subcategory"] == "Network2"
    assert payload["impact"] == "1"
    assert payload["urgency"] == "1"
    assert payload["message"] == critical_alert.subject
    assert payload["node"] == "jmeter-pv-us-ctl-vm01"
    assert payload["correlation_id"] == "opsramp:client_22568:122620538"
    assert payload["state"] == "1"
    assert payload["opened_at"] == "2021-07-24 17:19:12"
    assert "OpsRamp alert id" in payload["description"]
    assert json.loads(payload["comments"])["opsramp_alert_id"] == "122620538"


def test_recovery_alert_maps_to_the_resolved_state(recovery_alert: Alert) -> None:
    mapping = FieldMapping.load(Path("config/mapping/event.yaml"))
    payload = map_alert(recovery_alert, mapping)
    assert payload["state"] == "6"
    assert payload["urgency"] == "3"


def test_em_event_mapping_uses_the_message_key(critical_alert: Alert) -> None:
    mapping = FieldMapping.load(Path("config/mapping/em_event.yaml"))
    payload = map_alert(critical_alert, mapping)

    assert mapping.dedup_field == "message_key"
    assert payload["message_key"] == "opsramp:client_22568:122620538"
    assert payload["severity"] == "5"
    assert payload["resolution_state"] == "New"
    assert payload["source"] == "OpsRamp"


def test_incident_mapping_sets_the_contact_type(critical_alert: Alert) -> None:
    mapping = FieldMapping.load(Path("config/mapping/incident.yaml"))
    payload = map_alert(critical_alert, mapping)

    assert payload["contact_type"] == "automation"
    assert payload["short_description"] == critical_alert.subject
    assert payload["correlation_id"] == "opsramp:client_22568:122620538"
    assert payload["state"] == "1"


def test_ci_lookup_resolves_the_sys_id(critical_alert: Alert) -> None:
    mapping = FieldMapping.load(Path("config/mapping/event.yaml"))
    payload = map_alert(critical_alert, mapping, ci_resolver=lambda _host: "ci-sys-id")

    assert payload["cmdb_ci"] == "ci-sys-id"


def test_a_failed_ci_lookup_omits_the_column(critical_alert: Alert) -> None:
    mapping = FieldMapping.load(Path("config/mapping/event.yaml"))
    payload = map_alert(critical_alert, mapping, ci_resolver=lambda _host: None)

    assert "cmdb_ci" not in payload


def test_render_description_includes_the_state_transition(recovery_alert: Alert) -> None:
    rendered = render_description(recovery_alert)
    assert "Critical -> Ok" in rendered
    assert "122620540" in rendered


def test_a_mapping_without_a_table_is_rejected() -> None:
    with pytest.raises(ConfigError):
        FieldMapping.from_dict({"fields": {}})


def test_a_missing_mapping_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        FieldMapping.load(tmp_path / "nope.yaml")
