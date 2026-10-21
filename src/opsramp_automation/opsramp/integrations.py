"""OpsRamp Cloud API: custom integrations, integration events and activity.

The optional push path.  The polling path (:mod:`opsramp_automation.opsramp.alerts`)
remains the default because the ``$alert.*`` placeholder set is only partially
documented by the vendor.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ..errors import OpsRampError
from ..logging import get_logger
from .client import OpsRampClient
from .models import InstalledIntegration, IntegrationActivity, IntegrationEvent

log = get_logger(__name__)

#: The placeholders the vendor documents for the ``ALERT`` entity.
ALERT_PLACEHOLDERS = (
    "alert.serviceName",
    "alert.subject",
    "alert.currentState",
    "alert.resource.id",
    "alert.app",
)

#: The default push template: a JSON object with the documented placeholders.
DEFAULT_ALERT_PAYLOAD_TEMPLATE = json.dumps(
    {
        "serviceName": "$alert.serviceName",
        "resource": {"id": "$alert.resource.id"},
        "subject": "$alert.subject",
        "currentState": "$alert.currentState",
        "app": "$alert.app",
    },
    indent=2,
)


class IntegrationsApi:
    """Install custom integrations and wire alert events to a webhook."""

    def __init__(self, client: OpsRampClient) -> None:
        self.client = client

    @property
    def _base(self) -> str:
        return f"/v2/tenants/{self.client.settings.tenant_id}/integrations"

    # ------------------------------------------------------------------
    # installation
    # ------------------------------------------------------------------
    def install_custom_integration(
        self,
        display_name: str,
        *,
        category: str = "Custom",
        inbound_token: str | None = None,
    ) -> InstalledIntegration:
        """Install the ``CUSTOM`` integration and return the installed record."""
        inbound_config: dict[str, Any] = {}
        if inbound_token:
            inbound_config["authentication"] = {"authType": "WEBHOOK", "token": inbound_token}
        body: dict[str, Any] = {"displayName": display_name, "category": category}
        if inbound_config:
            body["inboundConfig"] = inbound_config
        payload = self.client.post(f"{self._base}/install/CUSTOM", json_body=body)
        if not isinstance(payload, dict) or not payload.get("id"):
            raise OpsRampError("Installing the custom integration returned no id")
        return InstalledIntegration.model_validate(payload)

    def find_installed(self, query: str | None = None) -> list[InstalledIntegration]:
        """Search the installed integrations."""
        params: dict[str, Any] = {}
        if query:
            params["queryString"] = query
        payload = self.client.get(f"{self._base}/installed/search", params=params)
        if not isinstance(payload, dict):
            return []
        results = payload.get("results") or []
        return [
            InstalledIntegration.model_validate(item) for item in results if isinstance(item, dict)
        ]

    def find_installed_by_name(self, display_name: str) -> InstalledIntegration | None:
        for integration in self.find_installed():
            if integration.displayName == display_name:
                return integration
        return None

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def create_alert_event(
        self,
        installed_intg_id: str,
        *,
        name: str,
        endpoint_uri: str,
        payload_template: str = DEFAULT_ALERT_PAYLOAD_TEMPLATE,
        headers: list[dict[str, str]] | None = None,
        base_uri: str | None = None,
        auth_type: str = "NONE",
        filter_criteria: dict[str, Any] | None = None,
    ) -> IntegrationEvent:
        """Create an ``ALERT`` entity integration event pointing at a webhook.

        ``payload_template`` is base64 encoded, as the vendor requires.
        """
        body: dict[str, Any] = {
            "name": name,
            "entity": "ALERT",
            "eventType": "NONE",
            "notifier": {
                "type": "REST_API",
                "authType": auth_type,
                "baseURI": base_uri or endpoint_uri,
            },
            "endPointURI": endpoint_uri,
            "headers": headers or [{"key": "Content-Type", "value": "application/json"}],
            "thirdPartyEventType": "POST",
            "payload": base64.b64encode(payload_template.encode("utf-8")).decode("ascii"),
        }
        if filter_criteria:
            # The vendor schema spells this "filterCritera".
            body["filterCriteria"] = filter_criteria
        payload = self.client.post(
            f"{self._base}/installed/{installed_intg_id}/event", json_body=body
        )
        if not isinstance(payload, dict):
            raise OpsRampError("Creating the integration event returned no data")
        return IntegrationEvent.model_validate(payload)

    def set_event_active(
        self, installed_intg_id: str, event_id: str, active: bool
    ) -> dict[str, Any]:
        """Activate or deactivate an integration event."""
        action = "activate" if active else "deactivate"
        payload = self.client.post(
            f"{self._base}/installed/{installed_intg_id}/event/{event_id}/{action}",
            json_body={},
        )
        return payload if isinstance(payload, dict) else {}

    def placeholders(self, entity_type: str = "ALERT") -> list[str]:
        """Return the placeholder list the vendor documents for an entity type.

        The endpoint is documented with a double slash (``/api/v2//tenants/...``)
        so both spellings are probed.
        """
        paths = (
            f"/v2/tenants/{self.client.settings.tenant_id}/integrations/events/placeHolders/{entity_type}",
            f"/v2//tenants/{self.client.settings.tenant_id}/integrations/events/placeHolders/{entity_type}",
        )
        last_error: Exception | None = None
        for path in paths:
            try:
                payload = self.client.get(path)
            except Exception as exc:  # noqa: BLE001 - the second spelling is the fallback
                last_error = exc
                continue
            if isinstance(payload, dict):
                values = payload.get("placeHolders") or payload.get("placeholders") or []
                return [str(item) for item in values]
            if isinstance(payload, list):
                return [str(item) for item in payload]
        if last_error is not None:
            raise OpsRampError(f"Unable to read the {entity_type} placeholder list") from last_error
        return []

    # ------------------------------------------------------------------
    # activity
    # ------------------------------------------------------------------
    def activity(
        self,
        installed_intg_id: str,
        *,
        status: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[IntegrationActivity]:
        """Read the delivery activity of an installed integration."""
        params: dict[str, Any] = {"installedintgId": installed_intg_id}
        if status:
            params["status"] = status
        if start:
            params["startTime"] = start
        if end:
            params["endTime"] = end
        payload = self.client.get(f"{self._base}/activity", params=params)
        if not isinstance(payload, dict):
            return []
        results = payload.get("results") or []
        return [
            IntegrationActivity.model_validate(item) for item in results if isinstance(item, dict)
        ]

    def retry_failed_events(
        self,
        installed_intg_id: str,
        *,
        start: str,
        end: str,
    ) -> dict[str, Any]:
        """Ask OpsRamp to replay failed events for a time range (max 2 hours)."""
        body = {"retry-events": [{"timeRange": {"startTime": start, "endTime": end}}]}
        payload = self.client.post(
            f"{self._base}/installed/{installed_intg_id}/events/re-try",
            json_body=body,
        )
        return payload if isinstance(payload, dict) else {}
