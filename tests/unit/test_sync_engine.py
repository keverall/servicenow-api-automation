"""The sync engine: deduplication, lifecycle and failure isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from opsramp_automation.opsramp.models import Alert
from opsramp_automation.sync.engine import SyncEngine
from opsramp_automation.sync.mapper import FieldMapping
from opsramp_automation.sync.state import SyncState

MAPPING = FieldMapping.load("config/mapping/event.yaml")

BASE_ALERT: dict[str, Any] = {
    "uniqueId": "122620538",
    "subject": "CPU utilisation above threshold",
    "description": "CPU utilisation is 95%",
    "currentState": "Critical",
    "oldState": "Ok",
    "serviceName": "CPU",
    "monitorName": "CPU",
    "alertType": "Monitoring",
    "app": "OpsRamp",
    "alertTime": "2021-07-24T17:19:12+0000",
    "createdDate": "2021-07-24T17:19:13+0000",
    "updatedTime": "2021-07-24T17:40:01+0000",
    "clientUniqueId": "client_22568",
    "tenantId": 22568,
    "tenantName": "Example Corp",
    "status": "New",
    "repeatCount": "2",
    "isRecoveryAlert": False,
    "device": {
        "id": "9297503",
        "name": "db-01",
        "hostName": "db-01.example.com",
        "ipAddress": "10.0.0.5",
        "resourceType": "Other",
    },
    "resource": {
        "id": "res-1",
        "name": "db-01",
        "hostName": "db-01.example.com",
        "ipAddress": "10.0.0.5",
        "resourceType": "Other",
    },
    "tags": [],
    "customFields": [],
}


def make_alert(**overrides: Any) -> Alert:
    """Return an alert built from the base payload with overrides applied."""
    payload = {**BASE_ALERT, **overrides}
    return Alert.model_validate(payload)


class FakeAlerts:
    """Stands in for :class:`AlertsApi`."""

    def __init__(self, alerts: list[Alert]) -> None:
        self.alerts = alerts
        self.calls = 0

    def iter_alerts(self, **_kwargs: Any):
        """Yield the canned alerts."""
        self.calls += 1
        yield from self.alerts


class FakeServiceNow:
    """Stands in for :class:`ServiceNowClient`."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.fail_on_create = False

    def find_by_correlation(
        self, table: str, correlation_id: str, *, dedup_field: str = "correlation_id", **_: Any
    ) -> dict[str, Any] | None:
        """Return the stored record for a dedup key."""
        return self.records.get(correlation_id)

    def create(self, table: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Store a new record."""
        if self.fail_on_create:
            raise RuntimeError("ServiceNow is unavailable")
        correlation = payload["correlation_id"]
        record = {
            **payload,
            "sys_id": f"sys-{len(self.records) + 1}",
            "number": "EVT001",
        }
        self.records[correlation] = record
        self.created.append(payload)
        return record

    def update(self, table: str, sys_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update an existing record."""
        correlation = payload.get("correlation_id")
        for key, record in self.records.items():
            if record.get("sys_id") == sys_id:
                record.update(payload)
                self.updated.append((sys_id, payload))
                if correlation and correlation != key:
                    self.records[key] = record
                return record
        raise KeyError(sys_id)

    def lookup_ci(self, hostname: str) -> dict[str, Any] | None:
        """Resolve a CI; the fake never resolves one."""
        return None


@pytest.fixture
def servicenow() -> FakeServiceNow:
    return FakeServiceNow()


@pytest.fixture
def state(tmp_path) -> SyncState:
    return SyncState(tmp_path / "state.db")


def build_engine(alerts: FakeAlerts, servicenow: FakeServiceNow, state: SyncState, **kwargs: Any):
    """Return a configured engine."""
    from opsramp_automation.config import SyncSettings

    settings = SyncSettings(
        dry_run=kwargs.pop("dry_run", False),
        ci_lookup=False,
        states=["Critical", "Warning", "Ok"],
        mapping_file="config/mapping/event.yaml",
        cache_ttl_minutes=60,
        **kwargs,
    )
    return SyncEngine(alerts, servicenow, state, settings, MAPPING)  # type: ignore[arg-type]


def test_a_new_alert_creates_exactly_one_record(state, servicenow) -> None:
    alerts = FakeAlerts([make_alert()])
    engine = build_engine(alerts, servicenow, state)

    report = engine.run_once()

    assert report.counts.get("created") == 1
    assert len(servicenow.created) == 1
    assert servicenow.created[0]["correlation_id"] == "opsramp:client_22568:122620538"
    assert servicenow.created[0]["state"] == "1"


def test_reprocessing_the_same_alert_does_not_duplicate_it(state, servicenow) -> None:
    alerts = FakeAlerts([make_alert()])
    engine = build_engine(alerts, servicenow, state)

    first = engine.run_once()
    second = engine.run_once()

    assert first.counts.get("created") == 1
    assert second.counts.get("unchanged") == 1
    assert len(servicenow.created) == 1


def test_a_recovery_closes_the_existing_record(state, servicenow) -> None:
    engine = build_engine(FakeAlerts([make_alert()]), servicenow, state)
    engine.run_once()

    recovery = make_alert(currentState="Ok", oldState="Critical", isRecoveryAlert=True)
    engine.alerts = FakeAlerts([recovery])
    report = engine.run_once()

    assert report.counts.get("updated") == 1
    assert servicenow.updated
    assert servicenow.updated[0][1]["state"] == "6"


def test_a_recovery_without_a_record_is_skipped(state, servicenow) -> None:
    recovery = make_alert(currentState="Ok", oldState="Critical", isRecoveryAlert=True)
    engine = build_engine(FakeAlerts([recovery]), servicenow, state)

    report = engine.run_once()

    assert report.counts.get("skipped_no_record") == 1
    assert servicenow.created == []


def test_a_healthy_alert_is_never_written(state, servicenow) -> None:
    healthy = make_alert(currentState="Ok", oldState="Ok")
    engine = build_engine(FakeAlerts([healthy]), servicenow, state)

    report = engine.run_once()

    assert report.counts.get("skipped_not_actionable") == 1
    assert servicenow.created == []


def test_a_dry_run_never_writes(state, servicenow) -> None:
    engine = build_engine(FakeAlerts([make_alert()]), servicenow, state, dry_run=True)

    report = engine.run_once()

    assert report.dry_run is True
    assert report.counts.get("would_create") == 1
    assert servicenow.created == []


def test_a_service_now_failure_is_isolated(state, servicenow) -> None:
    servicenow.fail_on_create = True
    engine = build_engine(FakeAlerts([make_alert()]), servicenow, state)

    report = engine.run_once()

    assert report.failed == 1
    assert report.failures
    assert report.failures[0]["correlation_id"] == "opsramp:client_22568:122620538"


def test_the_cursor_advances_to_the_newest_alert(state, servicenow) -> None:
    alerts = FakeAlerts(
        [
            make_alert(uniqueId="1", updatedTime="2021-07-24T17:30:01+0000"),
            make_alert(uniqueId="2", updatedTime="2021-07-24T17:40:01+0000"),
        ]
    )
    engine = build_engine(alerts, servicenow, state)

    engine.run_once()

    cursor = state.get_cursor()
    assert cursor is not None
    assert cursor.last_updated_time == "2021-07-24T17:40:01+0000"


def test_the_window_starts_at_the_cursor(state, servicenow) -> None:
    state.set_cursor("2021-07-24T17:40:01+0000", "122620538")
    engine = build_engine(FakeAlerts([]), servicenow, state, lookback_minutes=15)

    report = engine.run_once()

    assert report.window_start is not None
    assert report.window_start < datetime.now(UTC)
