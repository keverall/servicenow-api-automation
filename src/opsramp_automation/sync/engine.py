"""The alert -> ServiceNow sync engine.

The engine owns the idempotency contract:

* one OpsRamp alert (``clientUniqueId`` + ``uniqueId``) maps to exactly one
  ServiceNow record, enforced by the ``correlation_id`` dedup key;
* a recovery alert updates that record, it never creates a second one;
* a recovery for an alert that was never recorded is skipped;
* re-processing an alert is harmless because the dedup key is stable.
"""

from __future__ import annotations

import contextlib
import random
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import SyncSettings
from ..errors import SyncError
from ..logging import get_logger
from ..opsramp.alerts import AlertsApi, parse_opsramp_date
from ..opsramp.models import Alert
from ..servicenow.client import ServiceNowClient
from .mapper import (
    FieldMapping,
    correlation_id_for,
    is_problem_state,
    is_recovery,
    map_alert,
    normalise_state,
)
from .reporting import SyncReport
from .state import SyncState

log = get_logger(__name__)


class SyncEngine:
    """Poll OpsRamp alerts and keep the ServiceNow records in step."""

    def __init__(
        self,
        alerts: AlertsApi,
        servicenow: ServiceNowClient,
        state: SyncState,
        settings: SyncSettings,
        mapping: FieldMapping,
        *,
        ci_lookup: bool | None = None,
    ) -> None:
        self.alerts = alerts
        self.servicenow = servicenow
        self.state = state
        self.settings = settings
        self.mapping = mapping
        self.ci_lookup = settings.ci_lookup if ci_lookup is None else ci_lookup
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _states_filter(self) -> list[str]:
        """The alert states to request, normalised to the API's spelling."""
        return [normalise_state(state) for state in self.settings.states if state]

    def _resolve_ci(self, hostname: str) -> str | None:
        """Resolve a hostname to a CMDB CI sys_id."""
        if not self.ci_lookup or not hostname:
            return None
        try:
            record = self.servicenow.lookup_ci(hostname)
        except Exception as exc:  # noqa: BLE001 - a CI miss must not fail the alert
            log.warning("sync.ci_lookup_failed", hostname=hostname, error=str(exc))
            return None
        if not record:
            return None
        return str(record.get("sys_id")) if record.get("sys_id") else None

    def _window(self, now: datetime) -> tuple[datetime, datetime]:
        """Compute the search window from the cursor plus the lookback overlap."""
        lookback = timedelta(minutes=max(0, self.settings.lookback_minutes))
        cursor = self.state.get_cursor()
        start = now - lookback
        if cursor and cursor.last_updated_time:
            parsed = _parse_timestamp(cursor.last_updated_time)
            if parsed is not None:
                start = min(start, parsed - lookback)
        return start, now

    # ------------------------------------------------------------------
    # main entry points
    # ------------------------------------------------------------------
    def run_once(self) -> SyncReport:
        """Process the current window exactly once and return a report."""
        now = datetime.now(UTC)
        window_start, window_end = self._window(now)
        report = SyncReport(
            window_start=window_start,
            window_end=window_end,
            dry_run=self.settings.dry_run,
        )
        log.info(
            "sync.run_start",
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            dry_run=self.settings.dry_run,
        )

        latest_updated: datetime | None = None
        latest_updated_raw: str | None = None

        try:
            alerts = self.alerts.iter_alerts(
                client_ids=self.settings.client_ids or None,
                states=self._states_filter() or None,
                start_date=window_start,
                end_date=window_end,
                page_size=self.settings.page_size,
            )
            for alert in alerts:
                report.alerts_seen += 1
                updated = _parse_timestamp(alert.updatedTime or alert.alertTime or "")
                if updated is not None and (latest_updated is None or updated > latest_updated):
                    latest_updated = updated
                    latest_updated_raw = alert.updatedTime or alert.alertTime
                self._process_alert(alert, report)
        except Exception as exc:  # noqa: BLE001 - the whole search failed
            log.error("sync.search_failed", error=str(exc))
            report.bump("failed")
            report.failures.append({"error": f"alert search failed: {exc}"})

        if not self.settings.dry_run and latest_updated_raw:
            self.state.set_cursor(latest_updated_raw, latest_updated_raw)

        report.finished_at = datetime.now(UTC)
        log.info("sync.run_finished", **report.to_dict())
        return report

    def run_forever(self) -> None:
        """Poll on a fixed interval until a shutdown signal arrives."""
        self._install_signal_handlers()
        jitter = max(1, int(self.settings.poll_interval_s * 0.1))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                report = self.run_once()
                if report.failed:
                    log.warning("sync.run_partial_failure", failed=report.failed)
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                log.error("sync.run_failed", error=str(exc))
            elapsed = time.monotonic() - started
            sleep_for = max(1.0, self.settings.poll_interval_s - elapsed)
            sleep_for += random.uniform(0, jitter)  # noqa: S311 - jitter, not crypto
            log.info("sync.sleep", seconds=round(sleep_for, 1))
            if self._stop.wait(sleep_for):
                break
        log.info("sync.stopped")

    def stop(self) -> None:
        """Request a graceful shutdown of :meth:`run_forever`."""
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            log.info("sync.signal", signal=signum)
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError):  # not the main thread
                signal.signal(sig, _handler)

    # ------------------------------------------------------------------
    # per alert processing
    # ------------------------------------------------------------------
    def _process_alert(self, alert: Alert, report: SyncReport) -> None:
        correlation = correlation_id_for(alert, self.mapping)
        recovery = is_recovery(alert)
        try:
            cached = self.state.get_correlation(
                correlation, ttl_minutes=self.settings.cache_ttl_minutes
            )
            actionable = self._is_actionable(alert, recovery, cached is not None)
            if not actionable:
                report.bump("skipped_not_actionable")
                log.debug(
                    "sync.skipped",
                    alert_id=alert.uniqueId,
                    reason="not actionable",
                )
                return

            desired = map_alert(
                alert,
                self.mapping,
                ci_resolver=self._resolve_ci if self.ci_lookup else None,
            )

            record = self._find_record(correlation, cached, desired)
            if record is None:
                if recovery:
                    report.bump("skipped_no_record")
                    log.info(
                        "sync.recovery_without_record",
                        alert_id=alert.uniqueId,
                        correlation_id=correlation,
                    )
                    return
                self._create(alert, correlation, desired, report)
                return

            if recovery and not self.settings.close_on_recovery:
                report.bump("skipped_close_disabled")
                log.info("sync.close_disabled", alert_id=alert.uniqueId)
                return

            self._update(alert, correlation, desired, record, report)
        except Exception as exc:  # noqa: BLE001 - isolate per alert failures
            log.error(
                "sync.alert_failed",
                alert_id=alert.uniqueId,
                correlation_id=correlation,
                error=str(exc),
            )
            self.state.record_failure(correlation, alert.uniqueId, str(exc))
            report.bump("failed")
            report.failures.append(
                {
                    "opsramp_unique_id": alert.uniqueId,
                    "correlation_id": correlation,
                    "error": str(exc),
                }
            )

    def _is_actionable(self, alert: Alert, recovery: bool, cached: bool) -> bool:
        """Decide whether an alert warrants a ServiceNow interaction.

        A healthy alert (current state ``Ok``) that never had a problem state
        is not actionable and is skipped *before* any ServiceNow call, which
        keeps the request count within the OpsRamp rate limits.
        """
        if recovery:
            # A recovery matters when it closes a record we know about, or when
            # the alert previously was in a problem state.
            return cached or is_problem_state(alert.oldState)
        return is_problem_state(alert.currentState)

    def _find_record(
        self,
        correlation: str,
        cached: Any,
        desired: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return the existing ServiceNow record for a dedup key.

        The cached copy is used when it is fresh; otherwise ServiceNow is
        queried so that records changed or deleted outside this tool are
        detected.
        """
        record: dict[str, Any]
        if cached is not None:
            record = {
                "sys_id": cached.sys_id,
                "number": cached.number,
            }
            record.update(cached.payload or {})
            return record
        fields = list((desired or {}).keys())
        found = self.servicenow.find_by_correlation(
            self.mapping.table,
            correlation,
            dedup_field=self.mapping.dedup_field,
            fields=fields,
        )
        return found

    def _create(
        self,
        alert: Alert,
        correlation: str,
        desired: dict[str, Any],
        report: SyncReport,
    ) -> None:
        if self.settings.dry_run:
            report.bump("would_create")
            log.info("sync.dry_run_create", alert_id=alert.uniqueId, correlation_id=correlation)
            return
        created = self.servicenow.create(self.mapping.table, desired)
        sys_id = str(created.get("sys_id", ""))
        self.state.upsert_correlation(
            correlation,
            sys_id,
            number=created.get("number"),
            table_name=self.mapping.table,
            last_state=str(desired.get("state", "")),
            last_seen_alert_time=alert.updatedTime or alert.alertTime,
            payload=desired,
        )
        report.bump("created")
        log.info(
            "sync.created",
            alert_id=alert.uniqueId,
            correlation_id=correlation,
            number=created.get("number"),
        )

    def _update(
        self,
        alert: Alert,
        correlation: str,
        desired: dict[str, Any],
        record: dict[str, Any],
        report: SyncReport,
    ) -> None:
        sys_id = str(record.get("sys_id", ""))
        if not sys_id:
            report.bump("skipped_no_record")
            return

        changed = {
            key: value
            for key, value in desired.items()
            if _normalise(value) != _normalise(record.get(key))
        }
        if not changed:
            report.bump("unchanged")
            self.state.upsert_correlation(
                correlation,
                sys_id,
                number=record.get("number"),
                table_name=self.mapping.table,
                last_state=str(desired.get("state", "")),
                last_seen_alert_time=alert.updatedTime or alert.alertTime,
                payload=desired,
            )
            log.debug("sync.unchanged", alert_id=alert.uniqueId, correlation_id=correlation)
            return

        if self.settings.dry_run:
            report.bump("would_update")
            log.info(
                "sync.dry_run_update",
                alert_id=alert.uniqueId,
                correlation_id=correlation,
                fields=sorted(changed),
            )
            return

        updated = self.servicenow.update(self.mapping.table, sys_id, changed)
        self.state.upsert_correlation(
            correlation,
            sys_id,
            number=updated.get("number") or record.get("number"),
            table_name=self.mapping.table,
            last_state=str(desired.get("state", "")),
            last_seen_alert_time=alert.updatedTime or alert.alertTime,
            payload=desired,
        )
        report.bump("updated")
        log.info(
            "sync.updated",
            alert_id=alert.uniqueId,
            correlation_id=correlation,
            number=updated.get("number"),
            fields=sorted(changed),
        )


def _normalise(value: Any) -> str:
    """Compare values loosely: ServiceNow returns strings for numeric fields."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an OpsRamp timestamp, returning ``None`` when it cannot be read."""
    if not value:
        return None
    try:
        return parse_opsramp_date(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["SyncEngine", "SyncError"]
