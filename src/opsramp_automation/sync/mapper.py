"""Translate an OpsRamp alert into a ServiceNow payload using a YAML field map.

The mapping format is intentionally declarative so that a different ServiceNow
table (or a differently shaped instance) can be targeted without touching
Python code.  A field specification supports:

``const``      a fixed value
``from``       a dotted path into the alert (``subject``, ``device.hostName``)
               or the pseudo sources ``correlation``, ``state``/``recovery``
``fallback``   a second dotted path used when ``from`` resolves to nothing
``map``        a source-value -> target-value lookup
``default``    used when the source is missing or not present in ``map``
``template``   a ``str.format`` template over the alert context
``render``     a built-in renderer (``description`` or ``metadata``)
``truncate``   maximum string length
``ci_lookup``  resolve the field from a CMDB CI lookup
``format``     ``servicenow_datetime`` renders ``YYYY-MM-DD HH:MM:SS``
``on_open`` / ``on_recovery``  branch specs used for non-recovery / recovery alerts
``optional``   omit the field instead of writing ``null``
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError
from ..logging import get_logger
from ..opsramp.alerts import parse_opsramp_date
from ..opsramp.models import Alert
from .severity import is_recovery_state

log = get_logger(__name__)

SERVICENOW_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: States that mean the alert is actionable (a problem is present).
PROBLEM_STATES: frozenset[str] = frozenset({"critical", "major", "minor", "warning", "info"})

#: Canonical spelling of the OpsRamp alert states used by the ``states`` filter.
STATE_CANONICAL: dict[str, str] = {
    "ok": "Ok",
    "warning": "Warning",
    "critical": "Critical",
    "info": "Info",
    "major": "Major",
    "minor": "Minor",
    "clear": "Clear",
    "resolved": "Resolved",
}


def normalise_state(state: str) -> str:
    """Return the canonical OpsRamp spelling of an alert state."""
    return STATE_CANONICAL.get(state.strip().lower(), state.strip())


class _SafeDict(dict[str, Any]):
    """A format mapping that renders unknown keys as an empty string."""

    def __missing__(self, key: str) -> str:
        return ""


@dataclass
class FieldMapping:
    """A parsed YAML field map."""

    table: str
    dedup_field: str
    correlation_format: str
    correlation_max_length: int
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldMapping:
        if not isinstance(data, dict):
            raise ConfigError("A mapping file must contain a YAML mapping")
        table = data.get("table")
        if not table:
            raise ConfigError("A mapping file must define 'table'")
        dedup_field = data.get("dedup_field") or "correlation_id"
        correlation = data.get("correlation") or {}
        fields = data.get("fields") or {}
        if not isinstance(fields, dict):
            raise ConfigError("The 'fields' section of a mapping file must be a mapping")
        return cls(
            table=str(table),
            dedup_field=str(dedup_field),
            correlation_format=str(
                correlation.get("format") or "opsramp:{clientUniqueId}:{uniqueId}"
            ),
            correlation_max_length=int(correlation.get("max_length") or 1024),
            fields={
                str(k): (v if isinstance(v, dict) else {"const": v}) for k, v in fields.items()
            },
            raw=data,
        )

    @classmethod
    def load(cls, path: str | Path) -> FieldMapping:
        """Load and validate a mapping file."""
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigError(f"mapping file not found: {file_path}")
        with file_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        mapping = cls.from_dict(data)
        log.info("sync.mapping_loaded", path=str(file_path), table=mapping.table)
        return mapping


@dataclass
class MappingContext:
    """Everything a field specification may need while resolving."""

    alert: Alert
    is_recovery: bool
    correlation_id: str
    ci_resolver: Callable[[str], str | None] | None = None

    @property
    def state(self) -> str:
        return "recovery" if self.is_recovery else "open"

    def template_values(self) -> dict[str, Any]:
        alert = self.alert
        values: dict[str, Any] = {
            "uniqueId": alert.uniqueId,
            "subject": alert.subject or "",
            "description": alert.description or "",
            "currentState": alert.currentState or "",
            "oldState": alert.oldState or "",
            "state": self.state,
            "clientUniqueId": alert.clientUniqueId or "",
            "tenantName": alert.tenantName or "",
            "tenantId": alert.tenantId or "",
            "mspName": alert.mspName or "",
            "monitor": alert.monitorName or alert.metric or "",
            "service": alert.serviceName or "",
            "alertType": alert.alertType or "",
            "app": alert.app or "",
            "host": alert.host_name,
            "ip": alert.ip_address,
            "occurrences": alert.repeat_count,
            "correlation_id": self.correlation_id,
            "tags": json.dumps(alert.tags, default=str),
            "customFields": json.dumps(alert.customFields, default=str),
        }
        return values


# ----------------------------------------------------------------------
# public helpers
# ----------------------------------------------------------------------
def is_recovery(alert: Alert) -> bool:
    """Return ``True`` when the alert represents a recovery.

    An alert is a recovery when OpsRamp flags it as one, or when the state
    moved from a problem state into a recovered state.
    """
    if alert.isRecoveryAlert is True:
        return True
    if not is_recovery_state(alert.currentState):
        return False
    return not is_recovery_state(alert.oldState)


def is_problem_state(state: str | None) -> bool:
    """Return ``True`` when the state denotes an active problem."""
    if not state:
        return False
    return state.strip().lower() in PROBLEM_STATES


def correlation_id_for(alert: Alert, mapping: FieldMapping) -> str:
    """Compute the deterministic dedup key for an alert."""
    values = MappingContext(
        alert=alert, is_recovery=is_recovery(alert), correlation_id=""
    ).template_values()
    rendered = mapping.correlation_format.format_map(_SafeDict(values))
    return rendered[: mapping.correlation_max_length]


def render_description(alert: Alert) -> str:
    """Build the human readable ServiceNow description body."""
    lines = [
        alert.description or alert.subject or "",
        "",
        f"OpsRamp alert id: {alert.uniqueId}",
        f"State: {alert.oldState or 'n/a'} -> {alert.currentState or 'n/a'}",
        f"Resource: {alert.host_name or 'n/a'} ({alert.ip_address or 'no IP'})",
        f"Client: {alert.clientUniqueId or 'n/a'}",
        f"Tenant: {alert.tenantName or alert.tenantId or 'n/a'}",
        f"Monitor: {alert.monitorName or alert.metric or 'n/a'}",
        f"Service: {alert.serviceName or 'n/a'}",
        f"Occurrences: {alert.repeat_count}",
    ]
    if alert.app:
        lines.append(f"App: {alert.app}")
    metadata = {"tags": alert.tags, "customFields": alert.customFields}
    if metadata["tags"] or metadata["customFields"]:
        lines.extend(["", json.dumps(metadata, indent=2, default=str)])
    return "\n".join(lines).strip()


def render_metadata(alert: Alert) -> str:
    """Build the compact JSON metadata blob stored in comments / work notes."""
    return json.dumps(
        {
            "opsramp_alert_id": alert.uniqueId,
            "clientUniqueId": alert.clientUniqueId,
            "currentState": alert.currentState,
            "oldState": alert.oldState,
            "tags": alert.tags,
            "customFields": alert.customFields,
        },
        indent=2,
        default=str,
    )


def _resolve_path(alert: Alert, path: str) -> Any:
    """Walk a dotted path over the alert model."""
    current: Any = alert
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
            continue
        current = getattr(current, part, None)
    return current


def _format_servicenow_datetime(alert: Alert, value: Any) -> str | None:
    """Render an OpsRamp timestamp as a ServiceNow datetime.

    A malformed ``alertTime`` falls back to ``createdDate`` and finally to now.
    """
    for candidate in (value, alert.createdDate, alert.updatedTime):
        if not candidate:
            continue
        try:
            parsed = parse_opsramp_date(str(candidate))
        except ValueError:
            log.warning("sync.unparsable_timestamp", value=str(candidate))
            continue
        return parsed.astimezone(UTC).strftime(SERVICENOW_DATETIME_FORMAT)
    return datetime.now(UTC).strftime(SERVICENOW_DATETIME_FORMAT)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _resolve_spec(spec: dict[str, Any], ctx: MappingContext) -> Any:
    """Resolve a (possibly branched) field specification to a value."""
    if "on_open" in spec or "on_recovery" in spec:
        branch = spec.get("on_recovery") if ctx.is_recovery else spec.get("on_open")
        if branch is None:
            return None
        return _resolve_spec(branch, ctx)

    if "const" in spec:
        return spec["const"]

    if "template" in spec:
        rendered = str(spec["template"]).format_map(_SafeDict(ctx.template_values()))
        return rendered

    if "render" in spec:
        renderer = str(spec["render"])
        if renderer == "description":
            return render_description(ctx.alert)
        if renderer == "metadata":
            return render_metadata(ctx.alert)
        raise ConfigError(f"Unknown mapping renderer '{renderer}'")

    value: Any = None
    source = spec.get("from")
    if source is not None:
        value = _resolve_source(str(source), ctx)

    if _is_empty(value) and spec.get("fallback"):
        value = _resolve_source(str(spec["fallback"]), ctx)

    if value is None and "default" in spec:
        value = spec["default"]

    if isinstance(value, str) and "map" in spec:
        mapping = spec["map"] or {}
        if value in mapping:
            value = mapping[value]
        elif "default" in spec:
            value = spec["default"]

    return value


def _resolve_source(source: str, ctx: MappingContext) -> Any:
    """Resolve a source reference, including the pseudo sources."""
    if source == "correlation":
        return ctx.correlation_id
    if source in ("state", "recovery"):
        return ctx.state
    return _resolve_path(ctx.alert, source)


def map_alert(
    alert: Alert,
    mapping: FieldMapping,
    *,
    ci_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Render the ServiceNow payload for an alert.

    ``ci_resolver`` is called with the alert hostname when a field carries
    ``ci_lookup: true``; it must return the CMDB CI ``sys_id`` or ``None``.
    """
    recovery = is_recovery(alert)
    correlation = correlation_id_for(alert, mapping)
    ctx = MappingContext(
        alert=alert,
        is_recovery=recovery,
        correlation_id=correlation,
        ci_resolver=ci_resolver,
    )

    payload: dict[str, Any] = {}
    for field_name, spec in mapping.fields.items():
        value = _resolve_spec(spec, ctx)

        if spec.get("ci_lookup"):
            # A CI column holds a sys_id, never a host name: when the lookup
            # does not resolve, the field is omitted rather than written with
            # the hostname.
            hostname = ctx.alert.host_name
            value = None
            if hostname and ctx.ci_resolver is not None:
                value = ctx.ci_resolver(hostname) or None

        if spec.get("format") == "servicenow_datetime":
            value = _format_servicenow_datetime(ctx.alert, value)

        if _is_empty(value):
            if spec.get("optional"):
                continue
            if value is None and not spec.get("optional") and "const" in spec:
                # An explicit null constant is meaningful: keep it.
                payload[field_name] = None
                continue
            if value is None:
                continue

        limit = spec.get("truncate")
        if limit and isinstance(value, str):
            value = value[: int(limit)]

        payload[field_name] = value

    return payload
