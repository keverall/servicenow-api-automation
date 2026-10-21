"""Sync run reporting: a machine readable JSON artifact and a Markdown summary."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class SyncReport:
    """The outcome of a single sync run."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    dry_run: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    alerts_seen: int = 0

    def bump(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    @property
    def failed(self) -> int:
        return self.counts.get("failed", 0)

    @property
    def duration_s(self) -> float:
        end = self.finished_at or datetime.now(UTC)
        return max(0.0, (end - self.started_at).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": round(self.duration_s, 3),
            "dry_run": self.dry_run,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "alerts_seen": self.alerts_seen,
            "counts": dict(sorted(self.counts.items())),
            "failures": list(self.failures),
            "failed": self.failed,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_markdown(self) -> str:
        """Render a short summary suitable for ``$GITHUB_STEP_SUMMARY``."""
        lines = [
            "## OpsRamp to ServiceNow sync",
            "",
            f"- Dry run: **{'yes' if self.dry_run else 'no'}**",
            f"- Alerts seen: **{self.alerts_seen}**",
            f"- Duration: **{self.duration_s:.1f}s**",
        ]
        if self.window_start and self.window_end:
            lines.append(
                f"- Window: `{self.window_start.isoformat()}` to `{self.window_end.isoformat()}`"
            )
        lines.extend(["", "| Action | Count |", "| --- | --- |"])
        for key, value in sorted(self.counts.items()):
            lines.append(f"| {key} | {value} |")
        if self.failures:
            lines.extend(["", "### Failures", "", "| Alert | Error |", "| --- | --- |"])
            for failure in self.failures[:50]:
                alert_id = failure.get("opsramp_unique_id") or failure.get("correlation_id") or "?"
                error = str(failure.get("error", "")).replace("|", "\\|").replace("\n", " ")
                lines.append(f"| `{alert_id}` | {error[:200]} |")
        return "\n".join(lines) + "\n"
