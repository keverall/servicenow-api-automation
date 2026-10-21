"""ServiceNow record models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ServiceNowRecord(BaseModel):
    """A ServiceNow table record."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    sys_id: str
    number: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> ServiceNowRecord:
        return cls(
            sys_id=str(payload.get("sys_id", "")),
            number=payload.get("number"),
            raw=payload,
        )


class UpsertResult(StrEnum):
    """The outcome of a single alert upsert."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED_NO_RECORD = "skipped_no_record"
    SKIPPED_DRY_RUN = "skipped_dry_run"
    FAILED = "failed"


__all__ = ["ServiceNowRecord", "UpsertResult"]
