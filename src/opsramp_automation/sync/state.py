"""SQLite backed checkpoint, correlation cache and failure queue.

The state database gives the sync three properties:

* **resumability** - a cursor records how far the alert stream was consumed;
* **cheap deduplication** - a cache of ``correlation_id -> sys_id`` avoids a
  ServiceNow ``GET`` for every alert on every run;
* **isolation** - a per-alert failure is persisted and retried later instead
  of aborting the whole run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..logging import get_logger

log = get_logger(__name__)

DEFAULT_CURSOR = "alerts"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_cursor (
    name                 TEXT PRIMARY KEY,
    last_updated_time    TEXT,
    last_alert_unique_id TEXT,
    updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS correlation_cache (
    correlation_id       TEXT PRIMARY KEY,
    sys_id               TEXT,
    number               TEXT,
    table_name           TEXT,
    last_state           TEXT,
    last_seen_alert_time TEXT,
    payload              TEXT,
    updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS sync_failures (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id       TEXT,
    opsramp_unique_id    TEXT,
    error                TEXT,
    attempts             INTEGER DEFAULT 1,
    created_at           TEXT,
    updated_at           TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Cursor:
    """The position of the alert stream."""

    name: str
    last_updated_time: str | None = None
    last_alert_unique_id: str | None = None
    updated_at: str | None = None


@dataclass
class CachedRecord:
    """A cached mapping from the dedup key to a ServiceNow record."""

    correlation_id: str
    sys_id: str
    number: str | None = None
    table_name: str | None = None
    last_state: str | None = None
    last_seen_alert_time: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None


@dataclass
class Failure:
    """A persisted per-alert failure."""

    id: int
    correlation_id: str | None
    opsramp_unique_id: str | None
    error: str
    attempts: int
    created_at: str | None


class SyncState:
    """Persistent state for the alert sync."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # cursor
    # ------------------------------------------------------------------
    def get_cursor(self, name: str = DEFAULT_CURSOR) -> Cursor | None:
        row = self._conn.execute(
            "SELECT name, last_updated_time, last_alert_unique_id, updated_at "
            "FROM sync_cursor WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return Cursor(
            name=row["name"],
            last_updated_time=row["last_updated_time"],
            last_alert_unique_id=row["last_alert_unique_id"],
            updated_at=row["updated_at"],
        )

    def set_cursor(
        self,
        last_updated_time: str | None,
        last_alert_unique_id: str | None = None,
        name: str = DEFAULT_CURSOR,
    ) -> None:
        self._conn.execute(
            "INSERT INTO sync_cursor (name, last_updated_time, last_alert_unique_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_updated_time = excluded.last_updated_time, "
            "last_alert_unique_id = excluded.last_alert_unique_id, "
            "updated_at = excluded.updated_at",
            (name, last_updated_time, last_alert_unique_id, _now()),
        )
        self._conn.commit()

    def reset_cursor(self, name: str = DEFAULT_CURSOR) -> None:
        self._conn.execute("DELETE FROM sync_cursor WHERE name = ?", (name,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # correlation cache
    # ------------------------------------------------------------------
    def get_correlation(
        self,
        correlation_id: str,
        *,
        ttl_minutes: int | None = None,
    ) -> CachedRecord | None:
        """Return the cached record for a dedup key.

        When ``ttl_minutes`` is given, entries that have not been refreshed
        within that window are ignored so that stale or deleted records are
        re-verified against ServiceNow.
        """
        row = self._conn.execute(
            "SELECT correlation_id, sys_id, number, table_name, last_state, "
            "last_seen_alert_time, payload, updated_at FROM correlation_cache "
            "WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        if row is None:
            return None
        if ttl_minutes is not None and row["updated_at"]:
            try:
                updated = datetime.fromisoformat(row["updated_at"])
            except ValueError:
                updated = datetime.now(UTC)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated < datetime.now(UTC) - timedelta(minutes=ttl_minutes):
                return None
        payload: dict[str, Any] = {}
        if row["payload"]:
            try:
                loaded = json.loads(row["payload"])
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                payload = loaded
        return CachedRecord(
            correlation_id=row["correlation_id"],
            sys_id=row["sys_id"],
            number=row["number"],
            table_name=row["table_name"],
            last_state=row["last_state"],
            last_seen_alert_time=row["last_seen_alert_time"],
            payload=payload,
            updated_at=row["updated_at"],
        )

    def upsert_correlation(
        self,
        correlation_id: str,
        sys_id: str,
        *,
        number: str | None = None,
        table_name: str | None = None,
        last_state: str | None = None,
        last_seen_alert_time: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO correlation_cache "
            "(correlation_id, sys_id, number, table_name, last_state, last_seen_alert_time, "
            "payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(correlation_id) DO UPDATE SET sys_id = excluded.sys_id, "
            "number = excluded.number, table_name = excluded.table_name, "
            "last_state = excluded.last_state, "
            "last_seen_alert_time = excluded.last_seen_alert_time, "
            "payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (
                correlation_id,
                sys_id,
                number,
                table_name,
                last_state,
                last_seen_alert_time,
                json.dumps(payload, default=str) if payload is not None else None,
                _now(),
            ),
        )
        self._conn.commit()

    def delete_correlation(self, correlation_id: str) -> None:
        self._conn.execute(
            "DELETE FROM correlation_cache WHERE correlation_id = ?", (correlation_id,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # failures
    # ------------------------------------------------------------------
    def record_failure(
        self,
        correlation_id: str | None,
        opsramp_unique_id: str | None,
        error: str,
    ) -> None:
        """Persist (or increment) a failure for an alert."""
        existing = self._conn.execute(
            "SELECT id, attempts FROM sync_failures WHERE correlation_id IS ? "
            "AND opsramp_unique_id IS ?",
            (correlation_id, opsramp_unique_id),
        ).fetchone()
        if existing is not None:
            self._conn.execute(
                "UPDATE sync_failures SET error = ?, attempts = attempts + 1, "
                "updated_at = ? WHERE id = ?",
                (error[:2000], _now(), existing["id"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO sync_failures "
                "(correlation_id, opsramp_unique_id, error, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (correlation_id, opsramp_unique_id, error[:2000], _now(), _now()),
            )
        self._conn.commit()

    def pending_failures(self, max_attempts: int = 3) -> list[Failure]:
        """Return the failures that have not exhausted their retry budget."""
        rows = self._conn.execute(
            "SELECT id, correlation_id, opsramp_unique_id, error, attempts, created_at "
            "FROM sync_failures WHERE attempts < ? ORDER BY id",
            (max_attempts,),
        ).fetchall()
        return [
            Failure(
                id=row["id"],
                correlation_id=row["correlation_id"],
                opsramp_unique_id=row["opsramp_unique_id"],
                error=row["error"],
                attempts=row["attempts"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def clear_failure(self, failure_id: int) -> None:
        self._conn.execute("DELETE FROM sync_failures WHERE id = ?", (failure_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SyncState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
