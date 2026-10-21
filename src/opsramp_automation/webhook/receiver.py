"""Optional OpsRamp push receiver.

The polling path is the default because the vendor only partially documents
the ``$alert.*`` placeholders for ``ALERT`` integration events.  When the
placeholders have been validated against a live tenant, the integration can
push to this receiver instead, which validates the shared secret and hands the
alert to the same sync engine so that both paths share one dedup contract.

Requires the ``webhook`` extra::

    pip install 'opsramp-automation[webhook]'
    uvicorn opsramp_automation.webhook.receiver:app --port 8080
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..logging import get_logger

log = get_logger(__name__)

DEFAULT_TOKEN_HEADER = "x-opsramp-token"  # noqa: S105 - a header name, not a secret
DEFAULT_TOKEN_QUERY_PARAM = "vtoken"  # noqa: S105 - a query parameter name, not a secret


class WebhookAlert(BaseModel):
    """The subset of the OpsRamp alert the receiver needs."""

    model_config = ConfigDict(extra="allow")

    serviceName: str | None = None
    subject: str | None = None
    currentState: str | None = None
    app: str | None = None
    resource: dict[str, Any] = Field(default_factory=dict)


class AlertReceiver:
    """Holds the shared secret and the queue of received alerts."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("OPSRAMP_WEBHOOK_TOKEN", "")
        self._lock = threading.Lock()
        self.received: list[dict[str, Any]] = []

    def verify(self, header_value: str | None, query_value: str | None) -> bool:
        """Return ``True`` when the supplied secret matches."""
        if not self.token:
            # No secret configured: accept everything (development only).
            return True
        supplied = header_value or query_value or ""
        return supplied == self.token

    def accept(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.received.append(payload)
            if len(self.received) > 1000:
                del self.received[:-1000]

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.received)
            self.received.clear()
        return items


receiver = AlertReceiver()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    application = FastAPI(
        title="OpsRamp alert receiver",
        version="0.1.0",
        description="Optional push endpoint for the OpsRamp CUSTOM integration.",
    )

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/webhook/alert")
    async def alert_get(
        request: Request,
        x_opsramp_token: str | None = Header(default=None, alias=DEFAULT_TOKEN_HEADER),
    ) -> dict[str, Any]:
        """Accept an alert delivered with a GET (used by some webhook senders)."""
        token = request.query_params.get(DEFAULT_TOKEN_QUERY_PARAM)
        if not receiver.verify(x_opsramp_token, token):
            raise HTTPException(status_code=401, detail="invalid token")
        payload = dict(request.query_params)
        receiver.accept(payload)
        log.info("webhook.alert_received", transport="query")
        return {"accepted": True}

    @application.post("/webhook/alert")
    async def alert_post(
        request: Request,
        x_opsramp_token: str | None = Header(default=None, alias=DEFAULT_TOKEN_HEADER),
    ) -> dict[str, Any]:
        """Accept an alert delivered as JSON."""
        token = request.query_params.get(DEFAULT_TOKEN_QUERY_PARAM)
        if not receiver.verify(x_opsramp_token, token):
            raise HTTPException(status_code=401, detail="invalid token")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="the body must be a JSON object")
        receiver.accept(payload)
        log.info("webhook.alert_received", transport="json")
        return {"accepted": True}

    return application


app = create_app()
