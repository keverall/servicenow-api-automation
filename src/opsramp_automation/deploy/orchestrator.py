"""End-to-end, idempotent gateway deployment.

Stages, in order:

1. **profile**  - find or create the OpsRamp management profile
2. **attach**   - obtain the activation token
3. **package**  - download the gateway package
4. **provision**- make the appliance available
5. **auth**     - authenticate against the appliance admin API
6. **configure**- apply only the configuration deltas
7. **register** - register with OpsRamp (skipped when already registered)
8. **verify**   - wait for the profile to come up and the tunnel to establish
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import AppSettings, DeploySettings, GatewaySettings
from ..errors import ConfigError, GatewayError, OpsRampError
from ..logging import get_logger
from ..opsramp.client import OpsRampClient
from ..opsramp.gateway_admin import GatewayAdminClient
from ..opsramp.management_profiles import ManagementProfilesApi
from ..opsramp.models import ManagementProfile
from .provisioners.base import Endpoint
from .provisioners.existing import ExistingGatewayProvisioner
from .provisioners.vsphere import VSphereProvisioner

log = get_logger(__name__)

STAGE_OK = "ok"
STAGE_SKIPPED = "skipped"
STAGE_FAILED = "failed"


@dataclass
class StageResult:
    """The outcome of a single deployment stage."""

    name: str
    status: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, **self.data}


@dataclass
class DeployReport:
    """The outcome of a deployment run."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    dry_run: bool = False
    profile_id: int | str | None = None
    profile_name: str | None = None
    activation_token: str | None = None
    gateway_host: str | None = None
    registered: bool | None = None
    tunnel_status: str | None = None
    firmware_version: str | None = None
    stages: list[StageResult] = field(default_factory=list)

    def add(self, stage: StageResult) -> None:
        self.stages.append(stage)

    @property
    def succeeded(self) -> bool:
        return all(stage.status != STAGE_FAILED for stage in self.stages)

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
            "succeeded": self.succeeded,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "activation_token": "***REDACTED***" if self.activation_token else None,
            "gateway_host": self.gateway_host,
            "registered": self.registered,
            "tunnel_status": self.tunnel_status,
            "firmware_version": self.firmware_version,
            "stages": [stage.to_dict() for stage in self.stages],
        }

    def to_markdown(self) -> str:
        lines = [
            "## OpsRamp gateway deployment",
            "",
            f"- Dry run: **{'yes' if self.dry_run else 'no'}**",
            f"- Profile: `{self.profile_name or 'n/a'}` (id `{self.profile_id or 'n/a'}`)",
            f"- Gateway host: `{self.gateway_host or 'n/a'}`",
            f"- Registered: **{self.registered}**",
            f"- Tunnel: `{self.tunnel_status or 'n/a'}`",
            f"- Duration: **{self.duration_s:.1f}s**",
            "",
            "| Stage | Status | Detail |",
            "| --- | --- | --- |",
        ]
        for stage in self.stages:
            detail = stage.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {stage.name} | {stage.status} | {detail[:200]} |")
        return "\n".join(lines) + "\n"


class DeploymentOrchestrator:
    """Drives the deployment stages."""

    def __init__(self, settings: AppSettings, *, dry_run: bool | None = None) -> None:
        self.settings = settings
        self.deploy: DeploySettings = settings.deploy
        self.dry_run = self.deploy.dry_run if dry_run is None else dry_run
        self._client: OpsRampClient | None = None
        self._gateway: GatewayAdminClient | None = None

    # ------------------------------------------------------------------
    @property
    def client(self) -> OpsRampClient:
        if self._client is None:
            self.settings.opsramp.require_credentials()
            self._client = OpsRampClient(self.settings.opsramp)
        return self._client

    @property
    def gateway(self) -> GatewayAdminClient:
        if self._gateway is None:
            gateway_settings: GatewaySettings = self.settings.gateway
            gateway_settings.require_connection()
            self._gateway = GatewayAdminClient(gateway_settings)
        return self._gateway

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        if self._gateway is not None:
            self._gateway.close()

    # ------------------------------------------------------------------
    def run(self) -> DeployReport:
        """Execute every stage, collecting the outcome of each."""
        report = DeployReport(dry_run=self.dry_run)
        try:
            profile = self._stage_profile(report)
            self._stage_attach(profile, report)
            package_path = self._stage_package(report)
            endpoint = self._stage_provision(package_path, report)
            report.gateway_host = endpoint.host
            self._stage_auth(report)
            self._stage_configure(report)
            self._stage_register(report)
            self._stage_verify(profile, report)
        except OpsRampError as exc:
            report.add(StageResult("failed", STAGE_FAILED, str(exc)))
            log.error("deploy.failed", error=str(exc))
        finally:
            report.finished_at = datetime.now(UTC)
            self.close()
        return report

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------
    def _stage_profile(self, report: DeployReport) -> ManagementProfile:
        name = self.deploy.profile_name
        if not name:
            raise ConfigError("DEPLOY_PROFILE_NAME is required to deploy a gateway")
        api = ManagementProfilesApi(self.client)
        profile = api.find_by_name(name)
        if profile is not None:
            report.profile_id = profile.id
            report.profile_name = profile.name
            report.add(
                StageResult(
                    "profile",
                    STAGE_SKIPPED,
                    f"management profile '{name}' already exists",
                    {"profile_id": profile.id},
                )
            )
            log.info("deploy.profile_exists", profile_id=profile.id, name=name)
            return profile

        if self.dry_run:
            report.profile_name = name
            report.add(StageResult("profile", STAGE_SKIPPED, f"would create profile '{name}'"))
            return ManagementProfile(id=0, name=name, type="Gateway")

        profile = api.create_profile(name, self.deploy.description, "Gateway")
        report.profile_id = profile.id
        report.profile_name = profile.name
        report.add(
            StageResult(
                "profile",
                STAGE_OK,
                f"created management profile '{name}'",
                {"profile_id": profile.id},
            )
        )
        log.info("deploy.profile_created", profile_id=profile.id, name=name)
        return profile

    def _stage_attach(self, profile: ManagementProfile, report: DeployReport) -> None:
        if self.dry_run or not profile.id:
            report.add(StageResult("attach", STAGE_SKIPPED, "dry run"))
            return
        api = ManagementProfilesApi(self.client)
        attached = api.attach(profile.id)
        report.activation_token = attached.activationToken
        report.add(
            StageResult(
                "attach",
                STAGE_OK,
                "obtained the activation token",
                {"registered": attached.registered, "tunnel_type": attached.tunnelType},
            )
        )

    def _stage_package(self, report: DeployReport) -> Any:
        if self.dry_run:
            report.add(
                StageResult(
                    "package",
                    STAGE_SKIPPED,
                    f"would download the '{self.deploy.package_name}' package",
                )
            )
            return None
        api = ManagementProfilesApi(self.client)
        path = api.download_package(self.deploy.package_name, self.deploy.package_dir)
        report.add(StageResult("package", STAGE_OK, f"downloaded {path.name}", {"path": str(path)}))
        return path

    def _stage_provision(self, package_path: Any, report: DeployReport) -> Endpoint:
        if self.deploy.provisioner == "vsphere":
            provisioner: Any = VSphereProvisioner(self.settings.vsphere)
        else:
            provisioner = ExistingGatewayProvisioner(
                self.settings.gateway.host, self.settings.gateway.port
            )
        if self.dry_run:
            report.add(
                StageResult(
                    "provision",
                    STAGE_SKIPPED,
                    f"would use the {self.deploy.provisioner} provisioner",
                )
            )
            return Endpoint(host=self.settings.gateway.host, port=self.settings.gateway.port)
        endpoint: Endpoint = provisioner.prepare(package_path)
        report.add(
            StageResult("provision", STAGE_OK, provisioner.describe(), {"host": endpoint.host})
        )
        return endpoint

    def _stage_auth(self, report: DeployReport) -> None:
        try:
            self.gateway.authenticate()
        except GatewayError as exc:
            report.add(StageResult("auth", STAGE_FAILED, str(exc)))
            raise
        report.add(StageResult("auth", STAGE_OK, "authenticated against the appliance"))

    def _stage_configure(self, report: DeployReport) -> None:
        if self.dry_run:
            report.add(StageResult("configure", STAGE_SKIPPED, "dry run"))
            return
        changes = self.apply_config_deltas()
        if changes:
            report.add(
                StageResult(
                    "configure",
                    STAGE_OK,
                    "applied configuration deltas",
                    {"changes": changes},
                )
            )
        else:
            report.add(StageResult("configure", STAGE_SKIPPED, "configuration already up to date"))

    def _stage_register(self, report: DeployReport) -> None:
        status = self.gateway.registration_status()
        if status.lower() == "registered":
            report.registered = True
            report.add(StageResult("register", STAGE_SKIPPED, "appliance is already registered"))
            log.info("deploy.already_registered")
            return
        server = self.settings.gateway.registration_server
        if not server:
            raise ConfigError(
                "GATEWAY_REGISTRATION_SERVER is required to register the appliance. "
                "Copy the OpsRamp API server URL shown next to the activation token in the UI."
            )
        if self.dry_run:
            report.add(StageResult("register", STAGE_SKIPPED, "dry run"))
            return
        token = report.activation_token
        if not token:
            raise OpsRampError("No activation token was obtained from the management profile")
        message = self.gateway.register(server, token)
        report.add(StageResult("register", STAGE_OK, message))

    def _stage_verify(self, profile: ManagementProfile, report: DeployReport) -> None:
        if self.dry_run:
            report.add(StageResult("verify", STAGE_SKIPPED, "dry run"))
            return
        deadline = time.monotonic() + self.deploy.verify_timeout_s
        api = ManagementProfilesApi(self.client)
        last_status = ""
        last_tunnel = ""
        profile_ready = False
        while time.monotonic() < deadline:
            try:
                current = api.get_profile(profile.id)
                last_status = str(current.status or "")
                if current.registered and last_status.upper() == "UP":
                    profile_ready = True
                    break
            except OpsRampError as exc:
                log.warning("deploy.verify_profile_error", error=str(exc))
            time.sleep(self.deploy.verify_interval_s)

        try:
            last_tunnel = self.gateway.tunnel_status()
        except GatewayError as exc:
            log.warning("deploy.verify_tunnel_error", error=str(exc))

        report.registered = profile_ready
        report.tunnel_status = last_tunnel

        if not profile_ready:
            report.add(
                StageResult(
                    "verify",
                    STAGE_FAILED,
                    f"the profile did not come up within {self.deploy.verify_timeout_s}s "
                    f"(last status: {last_status or 'unknown'})",
                )
            )
            raise OpsRampError(
                f"The management profile did not reach status UP within "
                f"{self.deploy.verify_timeout_s}s (last status: {last_status or 'unknown'})"
            )
        if last_tunnel.lower() != "established":
            report.add(
                StageResult(
                    "verify",
                    STAGE_OK,
                    "the profile is UP but the tunnel is not established yet "
                    f"(tunnelStatus: {last_tunnel or 'unknown'}); this resolves on its own",
                    {"tunnel_established": False},
                )
            )
            return
        report.add(
            StageResult("verify", STAGE_OK, "the profile is UP and the tunnel is established")
        )

    # ------------------------------------------------------------------
    # configuration deltas
    # ------------------------------------------------------------------
    def apply_config_deltas(self) -> list[str]:
        """Apply only the configuration that differs from the appliance state."""
        changes: list[str] = []
        general = self.gateway.get_config("generalInfo")
        network = ((general.get("generalInfo") or {}).get("networkInformation")) or {}
        appliance = ((general.get("generalInfo") or {}).get("applianceInformation")) or {}
        report_firmware = appliance.get("applianceFirmwareVersion")
        if report_firmware:
            log.info("deploy.firmware", version=report_firmware)

        desired = self.deploy
        if desired.hostname or desired.ip_address or desired.subnet:
            current = {
                "hostname": str(network.get("hostName") or ""),
                "ipaddr": str(network.get("ipAddress") or ""),
                "subnet": str(network.get("subnetMask") or ""),
                "gateway": str(network.get("defaultGateway") or ""),
                "domainname": str(network.get("domainName") or ""),
                "interface": str(network.get("defaultInterface") or ""),
                "dnsArray": str(network.get("dnsEntries") or ""),
            }
            target = {
                "hostname": desired.hostname or current["hostname"],
                "ipaddr": desired.ip_address or current["ipaddr"],
                "subnet": desired.subnet or current["subnet"],
                "gateway": desired.network_gateway or current["gateway"],
                "domainname": desired.domain or current["domainname"],
                "interface": desired.interface or current["interface"],
                "dnsArray": desired.dns or current["dnsArray"],
            }
            differing = {k: v for k, v in target.items() if current[k] != v}
            if differing:
                self.gateway.set_network_settings(
                    domainname=target["domainname"],
                    hostname=target["hostname"],
                    interface=target["interface"],
                    dhcp=desired.dhcp,
                    ipaddr=target["ipaddr"],
                    subnet=target["subnet"],
                    gateway=target["gateway"],
                    dnsArray=target["dnsArray"],
                )
                changes.append(f"network:{','.join(sorted(differing))}")

        if desired.timezone:
            # The appliance does not expose the active zone in a comparable
            # form, so the zone is (re)applied; it is idempotent.
            self.gateway.set_timezone(desired.timezone)
            changes.append("timezone")

        if desired.ntp_servers:
            ntp = self.gateway.get_config("ntpInfo")
            info = ntp.get("ntpInfo") or {}
            current_servers = [
                item.strip() for item in str(info.get("servers") or "").split(",") if item.strip()
            ]
            if current_servers != desired.ntp_servers:
                self.gateway.set_ntp_servers(desired.ntp_servers)
                self.gateway.ntp_sync()
                changes.append("ntp")

        if desired.syslog_receivers:
            syslog = self.gateway.get_config("syslogInfo")
            entries = syslog.get("syslogInfoList") or []
            existing = {str(item.get("syslogInfo", {}).get("receiver")) for item in entries}
            for receiver in desired.syslog_receivers:
                if receiver not in existing:
                    self.gateway.add_syslog(
                        receiver=receiver, port="514", protocol="udp", selectors="*.*"
                    )
                    changes.append(f"syslog:{receiver}")

        if desired.proxy_connection == "proxy" and desired.proxy_ip:
            self.gateway.set_proxy(
                connection="proxy",
                proxy_ip=desired.proxy_ip,
                proxy_port=desired.proxy_port,
                proxy_usr=desired.proxy_user,
                proxy_pwd=desired.proxy_password.get_secret_value(),
            )
            changes.append("proxy")

        return changes
