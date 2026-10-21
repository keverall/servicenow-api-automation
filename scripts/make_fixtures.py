"""Generate the recorded OpsRamp alert fixtures.

The payloads mirror the shapes found in the OpsRamp alerts OpenAPI document
and the real-world edge cases the sync has to survive.
"""

from __future__ import annotations

import json
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "alerts"


def device(hostname: str, ip: str = "", device_id: str = "9297503") -> dict:
    return {
        "id": device_id,
        "name": hostname,
        "hostName": hostname,
        "ipAddress": ip,
        "type": "DEVICE",
        "aliasName": "",
        "resourceName": "",
        "resourceType": "Other",
    }


def resource(hostname: str, ip: str = "", tags: list | None = None) -> dict:
    return {
        "id": "336a5197-c8d8-46d0-9953-ebf95dd8046b",
        "hostName": hostname,
        "ipAddress": ip,
        "dns": "",
        "type": "Other",
        "state": "active",
        "make": "",
        "osName": "",
        "description": "",
        "source": "ALERT",
        "agentInstalled": False,
        "status": "UNDEFINED",
        "aliasName": "",
        "tags": tags or [],
        "name": hostname,
        "resourceName": "",
        "clientUniqueId": "client_22568",
        "resourceType": "Other",
        "generalInfo": {
            "name": hostname,
            "hostName": hostname,
            "resourceType": "Other",
            "createdTime": "2021-05-23T13:39:12+0000",
        },
    }


def alert(**overrides: object) -> dict:
    base = {
        "uniqueId": "122620538",
        "subject": "workflow alert Validation For Maintenance schedule Using JM-vas",
        "currentState": "Critical",
        "serviceName": "Network2",
        "problemArea": "Network2",
        "monitorName": "Network2",
        "metric": "Network2",
        "alertType": "Monitoring",
        "app": "OpsRamp",
        "component": "C",
        "alertTime": "2021-07-24T17:19:12+0000",
        "createdDate": "2021-07-24T17:19:13+0000",
        "updatedTime": "2021-07-24T17:40:01+0000",
        "clientUniqueId": "client_22568",
        "tenantId": 22568,
        "tenantName": "JMClient1605551845209",
        "mspName": "SQALab",
        "status": "New",
        "priority": "N/A",
        "acknowledged": False,
        "suppressed": False,
        "permanentlySuppressed": False,
        "closed": False,
        "ticketed": False,
        "isRecoveryAlert": False,
        "repeatCount": "2",
        "incidentId": "",
        "eventType": "ALERT",
        "entityType": "RESOURCE",
        "rtype": "RESOURCE",
        "rba": False,
        "inferenceAlert": False,
        "ignoreRecoveryNoise": False,
        "clientTechnology": "OpsRamp",
        "device": device("jmeter-pv-us-ctl-vm01"),
        "resource": resource("jmeter-pv-us-ctl-vm01"),
        "tags": [],
        "customFields": [],
        "description": "The Physical Memory Usage on the device is 56%.",
    }
    base.update(overrides)
    return base


FIXTURES: dict[str, dict] = {
    "01_critical_monitoring.json": alert(),
    "02_warning_monitoring.json": alert(
        uniqueId="122620539",
        currentState="Warning",
        oldState="Ok",
        subject="Disk usage above 80% on jmeter-pv-us-ctl-vm01",
        serviceName="Disk",
        monitorName="Disk",
        repeatCount="5",
    ),
    "03_recovery_flagged.json": alert(
        uniqueId="122620540",
        currentState="Ok",
        oldState="Critical",
        isRecoveryAlert=True,
        subject="workflow alert Validation For Maintenance schedule Using JM-vas",
        status="Closed",
    ),
    "04_recovery_by_old_state.json": alert(
        uniqueId="122620541",
        currentState="Ok",
        oldState="Warning",
        isRecoveryAlert=False,
        subject="Disk usage above 80% on jmeter-pv-us-ctl-vm01",
        serviceName="Disk",
    ),
    "05_empty_ip_address.json": alert(
        uniqueId="122620542",
        device=device("jmeter-pv-us-ctl-vm01", ip=""),
        resource=resource("jmeter-pv-us-ctl-vm01", ip=""),
    ),
    "06_resource_without_device.json": alert(
        uniqueId="122620543",
        device=None,
        resource=resource("db-primary-01", ip="10.10.0.5"),
        subject="Connection to db-primary-01 refused",
        serviceName="PostgreSQL",
    ),
    "07_unparsable_alert_time.json": alert(
        uniqueId="122620544",
        alertTime="not-a-timestamp",
        createdDate="2021-07-24T17:19:13+0000",
    ),
    "08_infrastructure_alert.json": alert(
        uniqueId="122620545",
        alertType="Infrastructure",
        currentState="Major",
        subject="RAID volume degraded on san-fabric-01",
        serviceName="Storage",
        monitorName="RAID",
        device=device("san-fabric-01", ip="10.20.0.9"),
        resource=resource("san-fabric-01", ip="10.20.0.9"),
    ),
    "09_tags_and_custom_fields.json": alert(
        uniqueId="122620546",
        tags=[{"name": "env", "value": "prod"}, {"name": "team", "value": "platform"}],
        customFields=[{"name": "runbook", "value": "https://wiki/runbook/disk"}],
        subject="Certificate expires in 7 days on edge-gw-02",
        serviceName="SSL",
        device=device("edge-gw-02", ip="10.30.0.4"),
        resource=resource("edge-gw-02", ip="10.30.0.4"),
    ),
    "10_no_hostname.json": alert(
        uniqueId="122620547",
        device=device("", ip=""),
        resource=resource("", ip=""),
        subject="Workflow alert without a resource",
    ),
    "11_other_client.json": alert(
        uniqueId="122620548",
        clientUniqueId="client_98765",
        tenantName="AcmeCorp",
        mspName="Acme MSP",
        device=device("acme-app-01", ip="192.168.5.10"),
        resource=resource("acme-app-01", ip="192.168.5.10"),
    ),
    "12_repeated_alert.json": alert(
        uniqueId="122620549",
        repeatCount="42",
        currentState="Critical",
        oldState="Warning",
        subject="Ping check failed for 42 consecutive attempts",
        serviceName="Ping",
        monitorName="Ping",
        device=device("ping-target-01", ip="10.40.0.7"),
        resource=resource("ping-target-01", ip="10.40.0.7"),
    ),
}


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, payload in FIXTURES.items():
        path = TARGET / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
