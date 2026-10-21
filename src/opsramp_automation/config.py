"""Configuration for every OpsRamp / ServiceNow interaction.

All settings are environment driven with a per-domain prefix so that the same
package can be pointed at different tenants without code changes:

======================  ==========================
Section                 Environment prefix
======================  ==========================
:class:`OpsRampSettings`       ``OPSRAMP_``
:class:`GatewaySettings`       ``GATEWAY_``
:class:`ServiceNowSettings`    ``SERVICENOW_``
:class:`SyncSettings`          ``SYNC_``
:class:`DeploySettings`        ``DEPLOY_``
:class:`VSphereSettings`       ``VSHERE_``
======================  ==========================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .errors import ConfigError

DEFAULT_BASE_URL = "https://api.opsramp.com"
DEFAULT_MAPPING_FILE = "config/mapping/event.yaml"


class _BaseSettings(BaseSettings):
    """Common pydantic-settings configuration.

    pydantic merges ``model_config`` across the MRO, so a subclass only has to
    declare its own ``env_prefix``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
        validate_assignment=True,
    )


def _split_csv(value: Any) -> Any:
    """Accept either a JSON array or a comma separated string for list fields."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return [item.strip() for item in stripped.strip("[]").split(",") if item.strip()]
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
            return [str(parsed)]
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


class OpsRampSettings(_BaseSettings):
    """Connection details for the OpsRamp cloud API."""

    model_config = SettingsConfigDict(env_prefix="OPSRAMP_")

    base_url: str = DEFAULT_BASE_URL
    tenant_id: str = ""
    client_id: str = Field(
        default="",
        validation_alias=AliasChoices("OPSRAMP_CLIENT_ID", "OPSRAMP_API_KEY"),
    )
    client_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPSRAMP_CLIENT_SECRET", "OPSRAMP_API_SECRET"),
    )
    scope: str = "global:manage"
    timeout_s: float = 30.0
    max_retries: int = 4
    token_refresh_margin_s: int = 60

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def require_credentials(self) -> None:
        if not self.tenant_id or not self.client_id or not self.client_secret.get_secret_value():
            raise ConfigError(
                "OpsRamp credentials are incomplete: set OPSRAMP_TENANT_ID, "
                "OPSRAMP_CLIENT_ID and OPSRAMP_CLIENT_SECRET"
            )


class GatewaySettings(_BaseSettings):
    """Connection details for the Classic gateway appliance admin API."""

    model_config = SettingsConfigDict(env_prefix="GATEWAY_")

    host: str = ""
    port: int = 5480
    username: str = "admin"
    password: SecretStr = SecretStr("")
    ca_bundle: Path | None = None
    verify_tls: bool = True
    registration_server: str = ""
    timeout_s: float = 30.0
    max_retries: int = 3

    @field_validator("ca_bundle", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        return value

    def require_connection(self) -> None:
        if not self.host:
            raise ConfigError("GATEWAY_HOST is required to reach the gateway appliance")
        if not self.password.get_secret_value():
            raise ConfigError("GATEWAY_PASSWORD is required to authenticate against the gateway")


class ServiceNowSettings(_BaseSettings):
    """Connection details for the ServiceNow Table API."""

    model_config = SettingsConfigDict(env_prefix="SERVICENOW_")

    instance: str = ""
    table: str = "event"
    username: str = ""
    password: SecretStr = SecretStr("")
    auth_mode: Literal["basic", "oauth"] = "basic"
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    sysparm_limit: int = 100
    min_interval_s: float = 0.0
    timeout_s: float = 30.0
    max_retries: int = 3

    @property
    def base_url(self) -> str:
        return f"https://{self.instance}.service-now.com"

    def require_credentials(self) -> None:
        if not self.instance:
            raise ConfigError("SERVICENOW_INSTANCE is required")
        if self.auth_mode == "oauth":
            if not self.client_id or not self.client_secret.get_secret_value():
                raise ConfigError(
                    "SERVICENOW_CLIENT_ID and SERVICENOW_CLIENT_SECRET are required for oauth auth"
                )
            return
        if not self.username or not self.password.get_secret_value():
            raise ConfigError("SERVICENOW_USER and SERVICENOW_PASSWORD are required")


class SyncSettings(_BaseSettings):
    """Controls the alert -> ServiceNow sync engine."""

    model_config = SettingsConfigDict(env_prefix="SYNC_")

    poll_interval_s: int = 300
    lookback_minutes: int = 15
    state_path: Path = Path(".opsramp-sync/state.db")
    dry_run: bool = True
    client_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # "Ok" is required so that recovery alerts are observed at all: the
    # OpsRamp ``states`` filter matches the *current* state of an alert, so a
    # filter of Critical/Warning alone would never return the recoveries.
    # Alerts that are simply healthy are skipped before any ServiceNow call.
    states: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Critical", "Warning", "Ok"]
    )
    close_on_recovery: bool = True
    ci_lookup: bool = True
    mapping_file: Path = Path(DEFAULT_MAPPING_FILE)
    max_attempts: int = 3
    cache_ttl_minutes: int = 60
    page_size: int = 100

    @field_validator("client_ids", "states", mode="before")
    @classmethod
    def _split_lists(cls, value: Any) -> Any:
        return _split_csv(value)


class VSphereSettings(_BaseSettings):
    """vSphere connection details used by the OVA provisioner."""

    model_config = SettingsConfigDict(env_prefix="VSHERE_")

    host: str = ""
    user: str = ""
    password: SecretStr = SecretStr("")
    datacenter: str = ""
    datastore: str = ""
    network: str = ""
    cluster: str = ""
    ovftool_path: str = "ovftool"
    insecure: bool = True
    guest_wait_s: int = 900

    def require_connection(self) -> None:
        missing = [
            name
            for name, value in (
                ("VSHERE_HOST", self.host),
                ("VSHERE_USER", self.user),
                ("VSHERE_PASSWORD", self.password.get_secret_value()),
                ("VSHERE_DATASTORE", self.datastore),
                ("VSHERE_NETWORK", self.network),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"vSphere provisioner is missing settings: {', '.join(missing)}")


class DeploySettings(_BaseSettings):
    """Controls the end-to-end gateway deployment."""

    model_config = SettingsConfigDict(env_prefix="DEPLOY_")

    profile_name: str = ""
    description: str = "Managed by opsramp-automation"
    package_name: Literal["ova", "iso", "windows", "google", "nextgen_iso", "nextgen_ova"] = "ova"
    provisioner: Literal["existing", "vsphere"] = "existing"
    package_dir: Path = Path(".opsramp-sync/packages")
    verify_timeout_s: int = 900
    verify_interval_s: int = 15
    dry_run: bool = False

    # Optional desired appliance configuration applied during deployment.
    hostname: str = ""
    domain: str = ""
    interface: str = ""
    ip_address: str = ""
    subnet: str = ""
    network_gateway: str = ""
    dns: str = ""
    dhcp: bool = False
    timezone: str = ""
    ntp_servers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    syslog_receivers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    proxy_connection: Literal["proxy", "direct"] = "direct"
    proxy_ip: str = ""
    proxy_port: str = ""
    proxy_user: str = ""
    proxy_password: SecretStr = SecretStr("")

    @field_validator("ntp_servers", "syslog_receivers", mode="before")
    @classmethod
    def _split_lists(cls, value: Any) -> Any:
        return _split_csv(value)


@dataclass
class AppSettings:
    """The complete, validated configuration used by the CLI."""

    opsramp: OpsRampSettings = field(default_factory=OpsRampSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    servicenow: ServiceNowSettings = field(default_factory=ServiceNowSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    deploy: DeploySettings = field(default_factory=DeploySettings)
    vsphere: VSphereSettings = field(default_factory=VSphereSettings)

    def describe(self) -> dict[str, Any]:
        """A redacted, log-safe summary of the configuration."""
        return {
            "opsramp": {
                "base_url": self.opsramp.base_url,
                "tenant_id": self.opsramp.tenant_id,
                "client_id": self.opsramp.client_id,
                "client_secret": "***REDACTED***" if self.opsramp.client_secret else "",
                "scope": self.opsramp.scope,
            },
            "gateway": {
                "host": self.gateway.host,
                "port": self.gateway.port,
                "username": self.gateway.username,
                "password": "***REDACTED***" if self.gateway.password else "",
                "ca_bundle": str(self.gateway.ca_bundle) if self.gateway.ca_bundle else None,
                "verify_tls": self.gateway.verify_tls,
                "registration_server": self.gateway.registration_server,
            },
            "servicenow": {
                "instance": self.servicenow.instance,
                "table": self.servicenow.table,
                "username": self.servicenow.username,
                "password": "***REDACTED***" if self.servicenow.password else "",
                "auth_mode": self.servicenow.auth_mode,
            },
            "sync": {
                "poll_interval_s": self.sync.poll_interval_s,
                "lookback_minutes": self.sync.lookback_minutes,
                "state_path": str(self.sync.state_path),
                "dry_run": self.sync.dry_run,
                "client_ids": self.sync.client_ids,
                "states": self.sync.states,
                "close_on_recovery": self.sync.close_on_recovery,
                "ci_lookup": self.sync.ci_lookup,
                "mapping_file": str(self.sync.mapping_file),
            },
            "deploy": {
                "profile_name": self.deploy.profile_name,
                "package_name": self.deploy.package_name,
                "provisioner": self.deploy.provisioner,
            },
        }


_SECTION_TYPES: dict[str, type[_BaseSettings]] = {
    "opsramp": OpsRampSettings,
    "gateway": GatewaySettings,
    "servicenow": ServiceNowSettings,
    "sync": SyncSettings,
    "deploy": DeploySettings,
    "vsphere": VSphereSettings,
}


def load_settings(config_file: Path | None = None) -> AppSettings:
    """Build :class:`AppSettings` from the environment, optionally overridden by YAML.

    The YAML file only needs to contain the sections that should differ from the
    environment defaults::

        opsramp:
          tenant_id: client_12345
        sync:
          dry_run: false
    """
    overrides: dict[str, Any] = {}
    if config_file is not None:
        path = Path(config_file)
        if not path.exists():
            raise ConfigError(f"configuration file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"configuration file must contain a mapping: {path}")
        for section, values in raw.items():
            if section not in _SECTION_TYPES:
                raise ConfigError(
                    f"unknown configuration section '{section}' in {path}; "
                    f"expected one of {', '.join(sorted(_SECTION_TYPES))}"
                )
            if values is None:
                continue
            if not isinstance(values, dict):
                raise ConfigError(f"configuration section '{section}' must be a mapping")
            overrides[section] = values

    def build(section: str, cls: type[_BaseSettings]) -> Any:
        section_overrides = overrides.get(section, {})
        return cls(**section_overrides)

    return AppSettings(
        opsramp=build("opsramp", OpsRampSettings),
        gateway=build("gateway", GatewaySettings),
        servicenow=build("servicenow", ServiceNowSettings),
        sync=build("sync", SyncSettings),
        deploy=build("deploy", DeploySettings),
        vsphere=build("vsphere", VSphereSettings),
    )
