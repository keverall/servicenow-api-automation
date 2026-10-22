"""The persistent sync state: cursor, correlation cache and failure log."""

from __future__ import annotations

from pathlib import Path

import pytest

from opsramp_automation.sync.state import SyncState


@pytest.fixture
def state(tmp_path: Path) -> SyncState:
    with SyncState(tmp_path / "state.db") as opened:
        yield opened


def test_a_fresh_state_has_no_cursor(state: SyncState) -> None:
    assert state.get_cursor() is None


def test_the_cursor_round_trips(state: SyncState) -> None:
    state.set_cursor("2021-07-24T17:40:01+0000", "122620538")

    cursor = state.get_cursor()

    assert cursor is not None
    assert cursor.last_updated_time == "2021-07-24T17:40:01+0000"
    assert cursor.last_alert_unique_id == "122620538"


def test_setting_the_cursor_again_updates_it(state: SyncState) -> None:
    state.set_cursor("2021-07-24T17:40:01+0000", "122620538")
    state.set_cursor("2021-07-24T18:00:00+0000", "122620539")

    cursor = state.get_cursor()

    assert cursor is not None
    assert cursor.last_updated_time == "2021-07-24T18:00:00+0000"


def test_the_cursor_can_be_reset(state: SyncState) -> None:
    state.set_cursor("2021-07-24T17:40:01+0000", "122620538")
    state.reset_cursor()

    assert state.get_cursor() is None


def test_the_correlation_cache_round_trips(state: SyncState) -> None:
    state.upsert_correlation(
        "opsramp:client_1:42",
        "sys-1",
        number="EVT001",
        table_name="event",
        last_state="1",
        payload={"state": "1", "message": "boom"},
    )

    cached = state.get_correlation("opsramp:client_1:42")

    assert cached is not None
    assert cached.sys_id == "sys-1"
    assert cached.number == "EVT001"
    assert cached.payload == {"state": "1", "message": "boom"}


def test_an_unknown_correlation_is_not_cached(state: SyncState) -> None:
    assert state.get_correlation("opsramp:client_1:999") is None


def test_a_correlation_can_be_deleted(state: SyncState) -> None:
    state.upsert_correlation("opsramp:client_1:42", "sys-1")
    state.delete_correlation("opsramp:client_1:42")

    assert state.get_correlation("opsramp:client_1:42") is None


def test_upserting_a_correlation_twice_keeps_one_row(state: SyncState) -> None:
    state.upsert_correlation("opsramp:client_1:42", "sys-1")
    state.upsert_correlation("opsramp:client_1:42", "sys-2", number="EVT002")

    cached = state.get_correlation("opsramp:client_1:42")

    assert cached is not None
    assert cached.sys_id == "sys-2"
    assert cached.number == "EVT002"


def test_failures_are_counted_and_can_be_cleared(state: SyncState) -> None:
    state.record_failure("opsramp:client_1:42", "122620538", "ServiceNow is unavailable")
    state.record_failure("opsramp:client_1:42", "122620538", "ServiceNow is unavailable again")

    pending = state.pending_failures(max_attempts=3)

    assert len(pending) == 1
    assert pending[0].attempts == 2

    state.clear_failure(pending[0].id)
    assert state.pending_failures(max_attempts=3) == []


def test_failures_exceeding_the_retry_budget_are_not_pending(state: SyncState) -> None:
    for _ in range(3):
        state.record_failure("opsramp:client_1:42", "122620538", "still failing")

    assert state.pending_failures(max_attempts=3) == []
