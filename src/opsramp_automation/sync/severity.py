"""Shared severity / urgency / impact mapping tables.

Kept in Python (rather than only in the YAML maps) so that the conversion
rules are unit-testable in isolation and can be reused by the CLI output.
"""

from __future__ import annotations

#: States that mean "the problem went away".
RECOVERY_STATES: frozenset[str] = frozenset({"ok", "resolved", "clear", "restored", "normal"})

#: OpsRamp state -> ServiceNow Event Management ``em_event.severity`` (integer).
OPSRAMP_STATE_TO_EM_SEVERITY: dict[str, int] = {
    "Critical": 5,
    "Major": 4,
    "Minor": 3,
    "Warning": 2,
    "Ok": 1,
    "Clear": 0,
}

#: OpsRamp state -> ITSM ``urgency`` / ``impact`` (1 = critical, 3 = moderate).
OPSRAMP_STATE_TO_SNOW_URGENCY: dict[str, str] = {
    "Critical": "1",
    "Major": "2",
    "Minor": "3",
    "Warning": "2",
    "Ok": "3",
    "Clear": "3",
}

#: OpsRamp state -> ITSM ``priority`` (1 = P1 critical, 4 = P4 low).
OPSRAMP_STATE_TO_SNOW_PRIORITY: dict[str, str] = {
    "Critical": "1",
    "Major": "2",
    "Minor": "3",
    "Warning": "3",
    "Ok": "4",
    "Clear": "4",
}

#: ServiceNow ITSM event/incident states.
STATE_OPEN = "1"
STATE_RESOLVED = "6"

#: ServiceNow Event Management resolution states.
EM_RESOLUTION_NEW = "New"
EM_RESOLUTION_CLOSING = "Closing"


def is_recovery_state(state: str | None) -> bool:
    """Return ``True`` when the state string denotes a recovered alert."""
    if not state:
        return False
    return state.strip().lower() in RECOVERY_STATES


def em_severity(state: str | None) -> int:
    """Map an OpsRamp state to the EM severity integer (default: minor)."""
    if not state:
        return OPSRAMP_STATE_TO_EM_SEVERITY["Minor"]
    return OPSRAMP_STATE_TO_EM_SEVERITY.get(state, OPSRAMP_STATE_TO_EM_SEVERITY["Minor"])


def snow_urgency(state: str | None) -> str:
    """Map an OpsRamp state to an ITSM urgency/impact value."""
    if not state:
        return OPSRAMP_STATE_TO_SNOW_URGENCY["Minor"]
    return OPSRAMP_STATE_TO_SNOW_URGENCY.get(state, OPSRAMP_STATE_TO_SNOW_URGENCY["Minor"])


def snow_priority(state: str | None) -> str:
    """Map an OpsRamp state to an ITSM priority value."""
    if not state:
        return OPSRAMP_STATE_TO_SNOW_PRIORITY["Minor"]
    return OPSRAMP_STATE_TO_SNOW_PRIORITY.get(state, OPSRAMP_STATE_TO_SNOW_PRIORITY["Minor"])
