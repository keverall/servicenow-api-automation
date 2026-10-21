"""Pydantic models for the OpsRamp Cloud API payloads.

Field names deliberately mirror the wire format (camelCase) so that a payload
can be round-tripped without a translation layer; snake_case helpers are
exposed as properties where they improve readability.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class AlertDevice(_Model):
    """The ``device`` block of an alert."""

    id: str | None = None
    name: str | None = None
    hostName: str | None = None
    ipAddress: str | None = None
    type: str | None = None
    aliasName: str | None = None
    resourceName: str | None = None
    resourceType: str | None = None

    @property
    def host_name(self) -> str:
        return self.hostName or self.name or ""


class AlertResource(_Model):
    """The ``resource`` block of an alert."""

    id: str | None = None
    name: str | None = None
    hostName: str | None = None
    ipAddress: str | None = None
    dns: str | None = None
    type: str | None = None
    state: str | None = None
    make: str | None = None
    osName: str | None = None
    description: str | None = None
    source: str | None = None
    status: str | None = None
    aliasName: str | None = None
    resourceName: str | None = None
    resourceType: str | None = None
    clientUniqueId: str | None = None
    tags: list[Any] = Field(default_factory=list)

    @property
    def host_name(self) -> str:
        return self.hostName or self.name or self.resourceName or ""


class Alert(_Model):
    """A single OpsRamp alert."""

    uniqueId: str
    subject: str | None = None
    description: str | None = None
    currentState: str | None = None
    oldState: str | None = None
    serviceName: str | None = None
    problemArea: str | None = None
    monitorName: str | None = None
    metric: str | None = None
    alertType: str | None = None
    app: str | None = None
    component: str | None = None
    alertTime: str | None = None
    createdDate: str | None = None
    updatedTime: str | None = None
    clientUniqueId: str | None = None
    tenantId: str | int | None = None
    tenantName: str | None = None
    mspName: str | None = None
    status: str | None = None
    priority: str | None = None
    acknowledged: bool | None = None
    suppressed: bool | None = None
    permanentlySuppressed: bool | None = None
    closed: bool | None = None
    ticketed: bool | None = None
    isRecoveryAlert: bool | None = None
    repeatCount: str | int | None = None
    incidentId: str | None = None
    eventType: str | None = None
    entityType: str | None = None
    rtype: str | None = None
    rba: bool | None = None
    inferenceAlert: bool | None = None
    ignoreRecoveryNoise: bool | None = None
    clientTechnology: str | None = None
    device: AlertDevice | None = None
    resource: AlertResource | None = None
    tags: list[Any] = Field(default_factory=list)
    customFields: list[Any] = Field(default_factory=list)

    @property
    def host_name(self) -> str:
        """Best available hostname: device first, then resource."""
        if self.device and (self.device.hostName or self.device.name):
            return self.device.host_name
        if self.resource:
            return self.resource.host_name
        return ""

    @property
    def ip_address(self) -> str:
        if self.device and self.device.ipAddress:
            return self.device.ipAddress
        if self.resource and self.resource.ipAddress:
            return self.resource.ipAddress
        return ""

    @property
    def repeat_count(self) -> int:
        try:
            return int(str(self.repeatCount))
        except (TypeError, ValueError):
            return 0


class AlertPage(_Model):
    """A page of alerts returned by ``/alerts/search``."""

    results: list[Alert] = Field(default_factory=list)
    totalResults: int | None = None
    pageNo: int | None = None
    pageSize: int | None = None
    totalPages: int | None = None
    nextPage: bool | None = None
    previousPageNo: int | None = None
    orderBy: str | None = None
    descendingOrder: bool | None = None


class ManagementProfileDevice(_Model):
    id: str | None = None
    uniqueId: str | None = None
    type: str | None = None
    clientUniqueId: str | None = None
    generalInfo: dict[str, Any] | None = None


class ManagementProfileService(_Model):
    name: str | None = None
    status: str | None = None


class ManagementProfile(_Model):
    """An OpsRamp management profile (a gateway or agent template)."""

    id: int | str
    name: str | None = None
    description: str | None = None
    type: str | None = None
    status: str | None = None
    createdDate: str | None = None
    statusUpdatedDate: str | None = None
    softwareVersion: str | None = None
    tunnelType: str | None = None
    registered: bool | None = None
    activationToken: str | None = None
    uuId: str | None = None
    device: ManagementProfileDevice | None = None
    services: list[ManagementProfileService] = Field(default_factory=list)
    outOfSyncTemplates: list[Any] = Field(default_factory=list)


class ManagementProfileSearchResult(_Model):
    results: list[ManagementProfile] = Field(default_factory=list)
    totalResults: int | None = None
    pageNo: int | None = None
    pageSize: int | None = None
    totalPages: int | None = None
    nextPage: bool | None = None


class DownloadPackage(_Model):
    """The signed download descriptor for a gateway package."""

    url: str
    expires_in: int | None = None


class InstalledIntegration(_Model):
    id: str
    displayName: str | None = None
    category: str | None = None
    status: str | None = None
    direction: str | None = None
    installedTime: str | None = None
    installedBy: str | None = None
    inboundConfig: dict[str, Any] | None = None
    integration: dict[str, Any] | None = None


class IntegrationEvent(_Model):
    id: str | None = None
    name: str | None = None
    entity: str | None = None
    eventType: str | None = None
    endPointURI: str | None = None
    thirdPartyEventType: str | None = None
    eventPayload: str | None = None
    notifier: dict[str, Any] | None = None
    headers: list[dict[str, Any]] = Field(default_factory=list)
    filterCriteria: dict[str, Any] | None = None
    active: bool | None = None


class IntegrationActivity(_Model):
    id: str | None = None
    name: str | None = None
    entity: str | None = None
    eventType: str | None = None
    status: str | None = None
    createdTime: str | None = None
    errorMessage: str | None = None
    payload: str | None = None
