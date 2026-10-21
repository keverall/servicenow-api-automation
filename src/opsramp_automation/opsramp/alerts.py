"""OpsRamp Cloud API: alert search and pagination.

The OpsRamp date filter is *not* ISO-8601: it is ``yyyy-MM-ddTHH:mm:ss 0000``
in GMT, with a literal space before the offset, which must be URL encoded as
``%20`` when it travels inside a query string.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from ..errors import OpsRampError
from ..logging import get_logger
from .client import OpsRampClient
from .models import Alert, AlertPage

log = get_logger(__name__)

DATE_FORMAT = "%Y-%m-%dT%H:%M:%S 0000"


def format_opsramp_date(value: datetime) -> str:
    """Render a datetime in the OpsRamp ``yyyy-MM-ddTHH:mm:ss 0000`` format."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(DATE_FORMAT)


def parse_opsramp_date(value: str) -> datetime:
    """Parse an OpsRamp timestamp, tolerating the ISO-8601 variants."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty timestamp")
    candidates = (
        DATE_FORMAT,
        "%Y-%m-%dT%H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in candidates:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise ValueError(f"unrecognised OpsRamp timestamp: {value!r}")


class AlertsApi:
    """Search and read OpsRamp alerts."""

    def __init__(self, client: OpsRampClient) -> None:
        self.client = client

    @property
    def _base(self) -> str:
        return f"/v2/tenants/{self.client.settings.tenant_id}/alerts"

    def search(
        self,
        *,
        client_ids: list[str] | None = None,
        states: list[str] | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        query_string: str | None = None,
        page_size: int = 100,
        page_no: int = 1,
        alert_time_base: str = "updated",
    ) -> AlertPage:
        """Run a single page of the alert search."""
        params: dict[str, Any] = {
            "pageNo": page_no,
            "pageSize": page_size,
            "sortName": "id",
            "isDescendingOrder": False,
            "alertTimeBase": alert_time_base,
        }
        if client_ids:
            params["clientIds"] = ",".join(client_ids)
        if states:
            params["states"] = ",".join(states)
        if start_date is not None:
            params["startDate"] = format_opsramp_date(start_date)
        if end_date is not None:
            params["endDate"] = format_opsramp_date(end_date)
        if query_string:
            params["queryString"] = query_string
        payload = self.client.get(f"{self._base}/search", params=params)
        if not isinstance(payload, dict):
            return AlertPage()
        return AlertPage.model_validate(payload)

    def iter_alerts(
        self,
        *,
        client_ids: list[str] | None = None,
        states: list[str] | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        query_string: str | None = None,
        page_size: int = 100,
    ) -> Iterator[Alert]:
        """Yield every alert matching the filter, following the pagination."""
        page_no = 1
        while True:
            page = self.search(
                client_ids=client_ids,
                states=states,
                start_date=start_date,
                end_date=end_date,
                query_string=query_string,
                page_size=page_size,
                page_no=page_no,
            )
            yield from page.results
            if not page.nextPage:
                return
            page_no += 1

    def get_alert(self, alert_id: str) -> Alert:
        """Fetch a single alert by its unique id."""
        payload = self.client.get(f"{self._base}/{alert_id}")
        if not isinstance(payload, dict):
            raise OpsRampError(f"Alert {alert_id} returned no data")
        return Alert.model_validate(payload)
