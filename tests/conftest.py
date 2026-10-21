"""Shared test fixtures.

No test in this suite makes a live API call: every outbound HTTP request is
intercepted with ``respx``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opsramp_automation.config import (
    GatewaySettings,
    OpsRampSettings,
    ServiceNowSettings,
    SyncSettings,
)
from opsramp_automation.opsramp.models import Alert

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "alerts"
MAPPING_DIR = Path(__file__).resolve().parents[1] / "config" / "mapping"

OPSRAMP_BASE = "https://api.opsramp.com"
GATEWAY_BASE = "https://10.0.0.5:5480"
SNOW_BASE = "https://dev12345.service-now.com"


@pytest.fixture
def alert_payloads() -> list[dict[str, Any]]:
    """Every recorded alert payload."""
    return [json.loads(path.read_text("utf-8")) for path in sorted(FIXTURE_DIR.glob("*.json"))]


@pytest.fixture
def alerts(alert_payloads: list[dict[str, Any]]) -> list[Alert]:
    """Every recorded alert, parsed into the model."""
    return [Alert.model_validate(payload) for payload in alert_payloads]


@pytest.fixture
def opsramp_settings() -> OpsRampSettings:
    return OpsRampSettings(
        base_url=OPSRAMP_BASE,
        tenant_id="client_22568",
        client_id="key",
        client_secret="secret",
        timeout_s=5.0,
        max_retries=1,
    )


@pytest.fixture
def gateway_settings() -> GatewaySettings:
    return GatewaySettings(
        host="10.0.0.5",
        port=5480,
        username="admin",
        password="Pass@1234",
        verify_tls=False,
        timeout_s=5.0,
        max_retries=1,
    )


@pytest.fixture
def servicenow_settings() -> ServiceNowSettings:
    return ServiceNowSettings(
        instance="dev12345",
        table="event",
        username="integration",
        password="secret",
        timeout_s=5.0,
        max_retries=1,
    )


@pytest.fixture
def sync_settings(tmp_path: Path) -> SyncSettings:
    return SyncSettings(
        poll_interval_s=1,
        lookback_minutes=15,
        state_path=tmp_path / "state.db",
        dry_run=False,
        client_ids=["client_22568"],
        states=["Critical", "Warning", "Ok"],
        ci_lookup=False,
        mapping_file=MAPPING_DIR / "event.yaml",
        cache_ttl_minutes=60,
    )


@pytest.fixture
def token_response() -> dict[str, Any]:
    return {"access_token": "token-abc", "expires_in": 7199, "token_type": "Bearer"}


@pytest.fixture
def mapping_paths() -> dict[str, Path]:
    return {
        "event": MAPPING_DIR / "event.yaml",
        "em_event": MAPPING_DIR / "em_event.yaml",
        "incident": MAPPING_DIR / "incident.yaml",
    }
