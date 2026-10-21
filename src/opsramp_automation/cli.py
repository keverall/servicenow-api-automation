"""``opsramp-gw`` command line interface.

Exit codes:

===  ==========================================
0    success
1    generic failure
2    configuration error
3    authentication error
4    rate limited
5    gateway unreachable
6    partial sync failure
===  ==========================================
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from .config import AppSettings, load_settings
from .errors import ConfigError, OpsRampError, SyncError, exit_code_for
from .logging import configure_logging, get_logger

log = get_logger("opsramp-gw")

app = typer.Typer(
    name="opsramp-gw",
    help="Deploy, administer and synchronise HPE OpsRamp gateways with ServiceNow.",
    no_args_is_help=True,
    add_completion=False,
)
profile_app = typer.Typer(
    name="profile", help="Manage OpsRamp management profiles.", no_args_is_help=True
)
admin_app = typer.Typer(
    name="admin", help="Administer a deployed gateway appliance.", no_args_is_help=True
)
alerts_app = typer.Typer(name="alerts", help="Inspect OpsRamp alerts.", no_args_is_help=True)
integration_app = typer.Typer(
    name="integration", help="Manage the optional OpsRamp push integration.", no_args_is_help=True
)

network_app = typer.Typer(name="network", help="Network settings.", no_args_is_help=True)
ntp_app = typer.Typer(name="ntp", help="NTP settings.", no_args_is_help=True)
syslog_app = typer.Typer(name="syslog", help="Syslog receivers.", no_args_is_help=True)
timezone_app = typer.Typer(name="timezone", help="Time zone.", no_args_is_help=True)
datetime_app = typer.Typer(name="datetime", help="Date and time.", no_args_is_help=True)
route_app = typer.Typer(name="route", help="Static routes.", no_args_is_help=True)
proxy_app = typer.Typer(name="proxy", help="External proxy.", no_args_is_help=True)
password_app = typer.Typer(name="password", help="Admin password.", no_args_is_help=True)

app.add_typer(profile_app)
app.add_typer(admin_app)
app.add_typer(alerts_app)
app.add_typer(integration_app)
admin_app.add_typer(network_app)
admin_app.add_typer(ntp_app)
admin_app.add_typer(syslog_app)
admin_app.add_typer(timezone_app)
admin_app.add_typer(datetime_app)
admin_app.add_typer(route_app)
admin_app.add_typer(proxy_app)
admin_app.add_typer(password_app)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _state(ctx: typer.Context) -> dict[str, Any]:
    return ctx.ensure_object(dict)


def _settings(ctx: typer.Context) -> AppSettings:
    state = _state(ctx)
    if "settings" not in state:
        config_file = state.get("config")
        state["settings"] = load_settings(config_file)
    settings: AppSettings = state["settings"]
    return settings


def _run(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a command body, mapping exceptions to the documented exit codes."""
    try:
        return func(*args, **kwargs)
    except OpsRampError as exc:
        log.error("command.failed", error=str(exc), exit_code=exc.exit_code)
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except typer.Exit:
        raise
    except Exception as exc:
        log.error("command.unexpected_error", error=str(exc))
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=exit_code_for(exc)) from exc


def _emit(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _parse_payload(payload_json: str | None) -> dict[str, Any]:
    if not payload_json:
        return {}
    try:
        parsed = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"--payload-json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("--payload-json must contain a JSON object")
    return parsed


def _pick(payload: dict[str, Any], key: str, override: Any) -> Any:
    """Prefer an explicit CLI flag, fall back to the JSON payload."""
    if override is not None:
        return override
    return payload.get(key)


# ----------------------------------------------------------------------
# root
# ----------------------------------------------------------------------
@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="YAML configuration file."),
    log_level: str = typer.Option("INFO", "--log-level", help="Logging level."),
    json_output: bool = typer.Option(True, "--json-output/--no-json-output", help="JSON logs."),
) -> None:
    """Global options shared by every command."""
    configure_logging(log_level, json_output=json_output)
    state = _state(ctx)
    state["config"] = config
    state["log_level"] = log_level
    state["json_output"] = json_output


# ----------------------------------------------------------------------
# deploy
# ----------------------------------------------------------------------
@app.command()
def deploy(
    ctx: typer.Context,
    profile_name: str | None = typer.Option(
        None, "--profile-name", help="Management profile name."
    ),
    package: str | None = typer.Option(None, "--package", help="Gateway package to deploy."),
    provisioner: str | None = typer.Option(None, "--provisioner", help="existing or vsphere."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not change anything."),
    report_path: Path | None = typer.Option(
        None, "--report", help="Write deploy-report.json here."
    ),
) -> None:
    """Deploy and register a gateway, end to end."""
    from .deploy.orchestrator import DeploymentOrchestrator

    def _run_deploy() -> None:
        settings = _settings(ctx)
        if profile_name:
            settings.deploy.profile_name = profile_name
        if package:
            settings.deploy.package_name = package  # type: ignore[assignment]
        if provisioner:
            settings.deploy.provisioner = provisioner  # type: ignore[assignment]
        orchestrator = DeploymentOrchestrator(settings, dry_run=dry_run)
        report = orchestrator.run()
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), "utf-8")
        _emit(report.to_dict())
        typer.echo(report.to_markdown())
        if not report.succeeded:
            raise SyncError("the deployment did not complete successfully")

    _run(_run_deploy)


# ----------------------------------------------------------------------
# profile
# ----------------------------------------------------------------------
def _profiles(ctx: typer.Context) -> Any:
    from .opsramp.client import OpsRampClient
    from .opsramp.management_profiles import ManagementProfilesApi

    settings = _settings(ctx)
    settings.opsramp.require_credentials()
    client = OpsRampClient(settings.opsramp)
    return client, ManagementProfilesApi(client)


@profile_app.command("list")
def profile_list(
    ctx: typer.Context,
    query: str | None = typer.Option(None, "--query", help="OpsRamp query string."),
) -> None:
    """List management profiles."""

    def _list() -> None:
        _, api = _profiles(ctx)
        _emit(
            [
                profile.model_dump(mode="json", exclude={"outOfSyncTemplates"})
                for profile in api.list_profiles(query)
            ]
        )

    _run(_list)


@profile_app.command("create")
def profile_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
    description: str = typer.Option("", "--description", help="Profile description."),
    profile_type: str = typer.Option("Gateway", "--type", help="Gateway or Agent."),
) -> None:
    """Create a management profile."""

    def _create() -> None:
        _, api = _profiles(ctx)
        profile = api.create_profile(name, description, profile_type)
        _emit(profile.model_dump(mode="json", exclude={"outOfSyncTemplates"}))

    _run(_create)


@profile_app.command("attach")
def profile_attach(
    ctx: typer.Context,
    profile_id: str = typer.Argument(..., help="Management profile id."),
) -> None:
    """Attach a gateway to a profile and return the activation token."""

    def _attach() -> None:
        _, api = _profiles(ctx)
        profile = api.attach(profile_id)
        _emit(profile.model_dump(mode="json", exclude={"outOfSyncTemplates"}))

    _run(_attach)


@profile_app.command("detach")
def profile_detach(
    ctx: typer.Context,
    profile_id: str = typer.Argument(..., help="Management profile id."),
) -> None:
    """Detach a gateway from a profile."""

    def _detach() -> None:
        _, api = _profiles(ctx)
        _emit(api.detach(profile_id))

    _run(_detach)


@profile_app.command("reconnect")
def profile_reconnect(
    ctx: typer.Context,
    profile_id: str = typer.Argument(..., help="Management profile id."),
) -> None:
    """Re-establish the gateway tunnel."""

    def _reconnect() -> None:
        _, api = _profiles(ctx)
        _emit(api.reconnect_tunnel(profile_id))

    _run(_reconnect)


@profile_app.command("services")
def profile_services(
    ctx: typer.Context,
    profile_id: str = typer.Argument(..., help="Management profile id."),
    service: list[str] = typer.Option(..., "--service", help="Service name, e.g. sipAgent."),
    enabled: bool = typer.Option(True, "--enable/--disable", help="Enable or disable."),
) -> None:
    """Enable or disable the agents of a management profile."""

    def _services() -> None:
        _, api = _profiles(ctx)
        profile = api.set_services(profile_id, dict.fromkeys(service, enabled))
        _emit(profile.model_dump(mode="json", exclude={"outOfSyncTemplates"}))

    _run(_services)


@profile_app.command("tunnel")
def profile_tunnel(
    ctx: typer.Context,
    profile_id: str = typer.Argument(..., help="Management profile id."),
) -> None:
    """Show the registration and tunnel state of a management profile."""

    def _tunnel() -> None:
        _, api = _profiles(ctx)
        profile = api.get_profile(profile_id)
        _emit(
            {
                "id": profile.id,
                "name": profile.name,
                "status": profile.status,
                "registered": profile.registered,
                "tunnelType": profile.tunnelType,
                "softwareVersion": profile.softwareVersion,
            }
        )

    _run(_tunnel)


@profile_app.command("out-of-sync")
def profile_out_of_sync(
    ctx: typer.Context,
    uuid: str | None = typer.Option(None, "--uuid", help="Restrict to one profile uuid."),
) -> None:
    """List management profiles whose deployed configuration drifted."""

    def _oos() -> None:
        _, api = _profiles(ctx)
        _emit(
            [
                p.model_dump(mode="json", exclude={"outOfSyncTemplates"})
                for p in api.find_out_of_sync(uuid)
            ]
        )

    _run(_oos)


# ----------------------------------------------------------------------
# admin
# ----------------------------------------------------------------------
def _gateway(ctx: typer.Context) -> Any:
    from .opsramp.gateway_admin import GatewayAdminClient

    settings = _settings(ctx)
    settings.gateway.require_connection()
    return GatewayAdminClient(settings.gateway)


@admin_app.command("status")
def admin_status(ctx: typer.Context) -> None:
    """Show the appliance status."""

    def _status() -> None:
        client = _gateway(ctx)
        try:
            _emit(client.get_config("generalInfo"))
        finally:
            client.close()

    _run(_status)


@admin_app.command("config")
def admin_config(
    ctx: typer.Context,
    info: str = typer.Option(
        "generalInfo",
        "--info",
        help="generalInfo|syslogInfo|ntpInfo|tzInfo|servicesInfo|routesInfo",
    ),
) -> None:
    """Print a configuration block of the appliance."""

    def _config() -> None:
        client = _gateway(ctx)
        try:
            _emit(client.get_config(info))
        finally:
            client.close()

    _run(_config)


@network_app.command("set")
def admin_network_set(
    ctx: typer.Context,
    hostname: str | None = typer.Option(None, "--hostname"),
    ip: str | None = typer.Option(None, "--ip"),
    subnet: str | None = typer.Option(None, "--subnet"),
    gateway: str | None = typer.Option(None, "--gateway"),
    dns: str | None = typer.Option(None, "--dns"),
    domain: str | None = typer.Option(None, "--domain"),
    interface: str | None = typer.Option(None, "--interface"),
    dhcp: bool = typer.Option(False, "--dhcp", help="Use DHCP instead of a static address."),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Apply the network settings of the appliance."""

    def _set() -> None:
        payload = _parse_payload(payload_json)
        client = _gateway(ctx)
        try:
            message = client.set_network_settings(
                domainname=str(_pick(payload, "domain", domain) or ""),
                hostname=str(_pick(payload, "hostname", hostname) or ""),
                interface=str(_pick(payload, "interface", interface) or ""),
                dhcp=bool(_pick(payload, "dhcp", dhcp)),
                ipaddr=str(_pick(payload, "ip", ip) or ""),
                subnet=str(_pick(payload, "subnet", subnet) or ""),
                gateway=str(_pick(payload, "gateway", gateway) or ""),
                dnsArray=str(_pick(payload, "dns", dns) or ""),
            )
            _emit({"success": message})
        finally:
            client.close()

    _run(_set)


@ntp_app.command("set")
def admin_ntp_set(
    ctx: typer.Context,
    server: list[str] = typer.Option([], "--server", help="NTP server (repeatable)."),
    sync: bool = typer.Option(False, "--sync", help="Synchronise immediately afterwards."),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Set the NTP servers of the appliance."""

    def _set() -> None:
        payload = _parse_payload(payload_json)
        servers = list(server) or list(payload.get("servers") or [])
        if not servers:
            raise ConfigError("at least one --server is required")
        client = _gateway(ctx)
        try:
            result = {"success": client.set_ntp_servers(servers)}
            if sync:
                result["sync"] = client.ntp_sync()
            _emit(result)
        finally:
            client.close()

    _run(_set)


@syslog_app.command("add")
def admin_syslog_add(
    ctx: typer.Context,
    receiver: str | None = typer.Option(None, "--receiver"),
    port: str | None = typer.Option(None, "--port"),
    protocol: str | None = typer.Option(None, "--protocol"),
    selectors: str | None = typer.Option(None, "--selectors"),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Add a syslog receiver."""

    def _add() -> None:
        payload = _parse_payload(payload_json)
        client = _gateway(ctx)
        try:
            _emit(
                {
                    "success": client.add_syslog(
                        receiver=str(_pick(payload, "receiver", receiver) or ""),
                        port=str(_pick(payload, "port", port) or "514"),
                        protocol=str(_pick(payload, "protocol", protocol) or "udp"),
                        selectors=str(_pick(payload, "selectors", selectors) or "*.*"),
                    )
                }
            )
        finally:
            client.close()

    _run(_add)


@syslog_app.command("update")
def admin_syslog_update(
    ctx: typer.Context,
    receiver: str | None = typer.Option(None, "--receiver"),
    port: str | None = typer.Option(None, "--port"),
    protocol: str | None = typer.Option(None, "--protocol"),
    selectors: str | None = typer.Option(None, "--selectors"),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Update a syslog receiver."""

    def _update() -> None:
        payload = _parse_payload(payload_json)
        client = _gateway(ctx)
        try:
            _emit(
                {
                    "success": client.update_syslog(
                        receiver=str(_pick(payload, "receiver", receiver) or ""),
                        port=str(_pick(payload, "port", port) or "514"),
                        protocol=str(_pick(payload, "protocol", protocol) or "udp"),
                        selectors=str(_pick(payload, "selectors", selectors) or "*.*"),
                    )
                }
            )
        finally:
            client.close()

    _run(_update)


@syslog_app.command("delete")
def admin_syslog_delete(
    ctx: typer.Context,
    receiver: str | None = typer.Option(None, "--receiver"),
    port: str | None = typer.Option(None, "--port"),
    protocol: str | None = typer.Option(None, "--protocol"),
    selectors: str | None = typer.Option(None, "--selectors"),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Remove a syslog receiver."""

    def _delete() -> None:
        payload = _parse_payload(payload_json)
        client = _gateway(ctx)
        try:
            _emit(
                {
                    "success": client.delete_syslog(
                        receiver=str(_pick(payload, "receiver", receiver) or ""),
                        port=str(_pick(payload, "port", port) or "514"),
                        protocol=str(_pick(payload, "protocol", protocol) or "udp"),
                        selectors=str(_pick(payload, "selectors", selectors) or "*.*"),
                    )
                }
            )
        finally:
            client.close()

    _run(_delete)


@syslog_app.command("reset")
def admin_syslog_reset(ctx: typer.Context) -> None:
    """Reset every syslog receiver."""

    def _reset() -> None:
        client = _gateway(ctx)
        try:
            _emit({"success": client.reset_syslog()})
        finally:
            client.close()

    _run(_reset)


@timezone_app.command("set")
def admin_timezone_set(
    ctx: typer.Context,
    tz: str | None = typer.Option(None, "--tz", help="Time zone, e.g. Alaska."),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Set the time zone of the appliance."""

    def _set() -> None:
        payload = _parse_payload(payload_json)
        zone = str(_pick(payload, "tz", tz) or _pick(payload, "tzs", None) or "")
        if not zone:
            raise ConfigError("--tz is required")
        client = _gateway(ctx)
        try:
            _emit({"success": client.set_timezone(zone)})
        finally:
            client.close()

    _run(_set)


@datetime_app.command("set")
def admin_datetime_set(
    ctx: typer.Context,
    date: str | None = typer.Option(None, "--date", help="Date as MM-DD-YYYY."),
    time: str | None = typer.Option(None, "--time", help="Time as HH:MM:SS."),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Set the date and time of the appliance."""

    def _set() -> None:
        payload = _parse_payload(payload_json)
        date_value = str(_pick(payload, "date", date) or "")
        time_value = str(_pick(payload, "time", time) or "")
        if not date_value or not time_value:
            raise ConfigError("--date and --time are required")
        parts = time_value.split(":")
        if len(parts) != 3:
            raise ConfigError("--time must be HH:MM:SS")
        client = _gateway(ctx)
        try:
            _emit(
                {
                    "success": client.set_datetime(
                        hour=parts[0], min=parts[1], sec=parts[2], date=date_value
                    )
                }
            )
        finally:
            client.close()

    _run(_set)


@route_app.command("add")
def admin_route_add(
    ctx: typer.Context,
    network: str | None = typer.Option(None, "--network", help="Destination network."),
    gateway: str | None = typer.Option(None, "--gateway", help="Next hop."),
    subnet: str | None = typer.Option(None, "--subnet", help="Subnet mask."),
    metric: str | None = typer.Option(None, "--metric", help="Route metric."),
    interface: str | None = typer.Option(None, "--interface", help="Outgoing interface."),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Add a static route."""

    def _add() -> None:
        payload = _parse_payload(payload_json)
        client = _gateway(ctx)
        try:
            _emit(
                {
                    "success": client.add_static_route(
                        srouteIP=str(_pick(payload, "network", network) or ""),
                        srouteGateway=str(_pick(payload, "gateway", gateway) or ""),
                        srouteSubnet=str(_pick(payload, "subnet", subnet) or ""),
                        srouteMetric=str(_pick(payload, "metric", metric) or "0"),
                        srouteInterface=str(_pick(payload, "interface", interface) or ""),
                    )
                }
            )
        finally:
            client.close()

    _run(_add)


@proxy_app.command("set")
def admin_proxy_set(
    ctx: typer.Context,
    direct: bool = typer.Option(False, "--direct", help="Bypass the proxy."),
    proxy_ip: str | None = typer.Option(None, "--proxy-ip"),
    proxy_port: str | None = typer.Option(None, "--proxy-port"),
    user: str | None = typer.Option(None, "--user"),
    password: str | None = typer.Option(None, "--password", envvar="GATEWAY_PROXY_PASSWORD"),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Configure the external proxy of the appliance."""

    def _set() -> None:
        payload = _parse_payload(payload_json)
        connection = "direct" if direct else str(_pick(payload, "connection", "proxy"))
        client = _gateway(ctx)
        try:
            _emit(
                {
                    "success": client.set_proxy(
                        connection=connection,
                        proxy_ip=str(_pick(payload, "proxy_ip", proxy_ip) or ""),
                        proxy_port=str(_pick(payload, "proxy_port", proxy_port) or ""),
                        proxy_usr=str(_pick(payload, "proxy_usr", user) or ""),
                        proxy_pwd=str(_pick(payload, "proxy_pwd", password) or ""),
                    )
                }
            )
        finally:
            client.close()

    _run(_set)


@password_app.command("change")
def admin_password_change(
    ctx: typer.Context,
    old: str | None = typer.Option(
        None, "--old", envvar="GATEWAY_OLD_PASSWORD", help="Current password."
    ),
    new: str | None = typer.Option(
        None, "--new", envvar="GATEWAY_NEW_PASSWORD", help="New password."
    ),
    username: str | None = typer.Option(None, "--username"),
) -> None:
    """Rotate the appliance admin password.

    The old and new passwords are read from the environment so that they do
    not end up in the shell history.  Remember to write the new value back to
    the CI secret, otherwise every later admin call fails with a 401.
    """

    def _change() -> None:
        if not old or not new:
            raise ConfigError(
                "GATEWAY_OLD_PASSWORD and GATEWAY_NEW_PASSWORD must be set to rotate the password"
            )
        settings = _settings(ctx)
        client = _gateway(ctx)
        try:
            message = client.change_password(
                username=username or settings.gateway.username,
                oldpasswd=old,
                newpasswd=new,
            )
            _emit({"success": message, "rotated": True})
        finally:
            client.close()

    _run(_change)


@admin_app.command("service")
def admin_service(
    ctx: typer.Context,
    service: str = typer.Argument(..., help="network|squid|postgresql|ntp"),
    action: str = typer.Argument(..., help="start|stop|status|restart"),
) -> None:
    """Control a monitorable appliance service."""

    def _service() -> None:
        client = _gateway(ctx)
        try:
            _emit({"success": client.monit_service(service, action)})
        finally:
            client.close()

    _run(_service)


@admin_app.command("restart")
def admin_restart(
    ctx: typer.Context,
    target: str = typer.Argument("all", help="all"),
) -> None:
    """Restart every appliance service."""

    def _restart() -> None:
        if target != "all":
            raise ConfigError("the only supported restart target is 'all'")
        client = _gateway(ctx)
        try:
            _emit({"success": client.restart_all_services()})
        finally:
            client.close()

    _run(_restart)


@admin_app.command("register")
def admin_register(
    ctx: typer.Context,
    server: str | None = typer.Option(None, "--server", help="OpsRamp API server URL."),
    activation_token: str | None = typer.Option(None, "--activation-token"),
    payload_json: str | None = typer.Option(None, "--payload-json"),
) -> None:
    """Register the appliance with OpsRamp."""

    def _register() -> None:
        payload = _parse_payload(payload_json)
        settings = _settings(ctx)
        server_value = str(_pick(payload, "server", server) or settings.gateway.registration_server)
        token = str(_pick(payload, "activation_token", activation_token) or "")
        if not token:
            raise ConfigError("--activation-token is required")
        client = _gateway(ctx)
        try:
            _emit({"success": client.register(server_value, token)})
        finally:
            client.close()

    _run(_register)


# ----------------------------------------------------------------------
# alerts
# ----------------------------------------------------------------------
@alerts_app.command("list")
def alerts_list(
    ctx: typer.Context,
    client_id: list[str] = typer.Option([], "--client", help="Client id (repeatable)."),
    state: list[str] = typer.Option([], "--state", help="Alert state (repeatable)."),
    since: str | None = typer.Option(None, "--since", help="Start date, YYYY-MM-DD."),
    limit: int = typer.Option(50, "--limit", help="Maximum number of alerts."),
) -> None:
    """List recent alerts."""

    def _list() -> None:
        from .opsramp.alerts import AlertsApi
        from .opsramp.client import OpsRampClient

        settings = _settings(ctx)
        settings.opsramp.require_credentials()
        client = OpsRampClient(settings.opsramp)
        try:
            start = None
            if since:
                start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC)
            alerts = AlertsApi(client).iter_alerts(
                client_ids=list(client_id) or None,
                states=list(state) or None,
                start_date=start,
                end_date=datetime.now(UTC),
                page_size=min(limit, 100),
            )
            results = []
            for index, alert in enumerate(alerts):
                if index >= limit:
                    break
                results.append(alert.model_dump(mode="json"))
            _emit(results)
        finally:
            client.close()

    _run(_list)


# ----------------------------------------------------------------------
# sync
# ----------------------------------------------------------------------
@app.command()
def sync(
    ctx: typer.Context,
    once: bool = typer.Option(True, "--once/--daemon", help="Run once or continuously."),
    dry_run: bool | None = typer.Option(None, "--dry-run", help="Override SYNC_DRY_RUN."),
    report_path: Path | None = typer.Option(None, "--report", help="Write sync-report.json here."),
    mapping: Path | None = typer.Option(None, "--mapping", help="Override the mapping file."),
) -> None:
    """Synchronise OpsRamp alerts into ServiceNow."""

    def _sync() -> None:
        from .opsramp.alerts import AlertsApi
        from .opsramp.client import OpsRampClient
        from .servicenow.client import ServiceNowClient
        from .sync.engine import SyncEngine
        from .sync.mapper import FieldMapping
        from .sync.state import SyncState

        settings = _settings(ctx)
        settings.opsramp.require_credentials()
        settings.servicenow.require_credentials()
        if dry_run is not None:
            settings.sync.dry_run = dry_run
        if mapping is not None:
            settings.sync.mapping_file = mapping

        field_mapping = FieldMapping.load(settings.sync.mapping_file)
        opsramp_client = OpsRampClient(settings.opsramp)
        servicenow_client = ServiceNowClient(settings.servicenow)
        state = SyncState(settings.sync.state_path)
        engine = SyncEngine(
            AlertsApi(opsramp_client),
            servicenow_client,
            state,
            settings.sync,
            field_mapping,
        )
        try:
            if once:
                report = engine.run_once()
                if report_path:
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(report.to_json(), "utf-8")
                _emit(report.to_dict())
                typer.echo(report.to_markdown())
                if report.failed:
                    raise SyncError(f"{report.failed} alert(s) failed to synchronise")
            else:
                engine.run_forever()
        finally:
            state.close()
            opsramp_client.close()
            servicenow_client.close()

    _run(_sync)


# ----------------------------------------------------------------------
# integration
# ----------------------------------------------------------------------
@integration_app.command("apply")
def integration_apply(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Display name of the custom integration."),
    webhook_url: str = typer.Option(..., "--webhook-url", help="Our webhook endpoint."),
    payload_template: Path | None = typer.Option(None, "--payload-template", help="JSON template."),
    activate: bool = typer.Option(True, "--activate/--no-activate"),
) -> None:
    """Install the custom integration and create the ALERT event."""

    def _apply() -> None:
        from .opsramp.client import OpsRampClient
        from .opsramp.integrations import DEFAULT_ALERT_PAYLOAD_TEMPLATE, IntegrationsApi

        settings = _settings(ctx)
        settings.opsramp.require_credentials()
        client = OpsRampClient(settings.opsramp)
        try:
            api = IntegrationsApi(client)
            integration = api.find_installed_by_name(name) or api.install_custom_integration(name)
            template = DEFAULT_ALERT_PAYLOAD_TEMPLATE
            if payload_template:
                template = Path(payload_template).read_text("utf-8")
            event = api.create_alert_event(
                integration.id,
                name=f"{name} alert event",
                endpoint_uri=webhook_url,
                payload_template=template,
            )
            if activate and event.id:
                api.set_event_active(integration.id, event.id, True)
            _emit(
                {
                    "integration_id": integration.id,
                    "event_id": event.id,
                    "active": activate,
                }
            )
        finally:
            client.close()

    _run(_apply)


@integration_app.command("activity")
def integration_activity(
    ctx: typer.Context,
    installed_id: str = typer.Option(..., "--installed-id"),
    status: str | None = typer.Option(None, "--status", help="SUCCESS or FAILED."),
    since: str | None = typer.Option(None, "--since", help="Start of the window (ISO-8601)."),
    until: str | None = typer.Option(None, "--until", help="End of the window (ISO-8601)."),
) -> None:
    """Show the delivery activity of an installed integration."""

    def _activity() -> None:
        from .opsramp.client import OpsRampClient
        from .opsramp.integrations import IntegrationsApi

        settings = _settings(ctx)
        settings.opsramp.require_credentials()
        client = OpsRampClient(settings.opsramp)
        try:
            api = IntegrationsApi(client)
            activity = api.activity(installed_id, status=status, start=since, end=until)
            _emit([item.model_dump(mode="json") for item in activity])
        finally:
            client.close()

    _run(_activity)


@integration_app.command("retry")
def integration_retry(
    ctx: typer.Context,
    installed_id: str = typer.Option(..., "--installed-id"),
    since: str = typer.Option(..., "--since", help="Start of the window (ISO-8601)."),
    until: str = typer.Option(..., "--until", help="End of the window (ISO-8601)."),
) -> None:
    """Replay failed integration events for a time range (max two hours)."""

    def _retry() -> None:
        from .opsramp.client import OpsRampClient
        from .opsramp.integrations import IntegrationsApi

        settings = _settings(ctx)
        settings.opsramp.require_credentials()
        client = OpsRampClient(settings.opsramp)
        try:
            api = IntegrationsApi(client)
            _emit(api.retry_failed_events(installed_id, start=since, end=until))
        finally:
            client.close()

    _run(_retry)


# ----------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------
@app.command()
def doctor(
    ctx: typer.Context,
    mapping: Path | None = typer.Option(None, "--mapping", help="Override the mapping file."),
) -> None:
    """Validate the target ServiceNow table and the appliance before a sync.

    Field names and choice values differ between ServiceNow instances, so this
    command fails fast with a precise list of what has to be fixed.
    """

    def _doctor() -> None:
        from .opsramp.client import OpsRampClient
        from .opsramp.management_profiles import ManagementProfilesApi
        from .servicenow.client import ServiceNowClient
        from .sync.mapper import FieldMapping

        settings = _settings(ctx)
        problems: list[str] = []
        checks: dict[str, Any] = {}

        try:
            settings.opsramp.require_credentials()
            client = OpsRampClient(settings.opsramp)
            try:
                profiles = ManagementProfilesApi(client).list_profiles()
                checks["opsramp"] = {"ok": True, "management_profiles": len(profiles)}
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 - the doctor reports, it does not raise
            problems.append(f"OpsRamp API not usable: {exc}")
            checks["opsramp"] = {"ok": False, "error": str(exc)}

        if settings.gateway.host:
            try:
                from .opsramp.gateway_admin import GatewayAdminClient

                gateway = GatewayAdminClient(settings.gateway)
                try:
                    gateway.get_config("generalInfo")
                    checks["gateway"] = {"ok": True, "host": settings.gateway.host}
                finally:
                    gateway.close()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"Gateway appliance not usable: {exc}")
                checks["gateway"] = {"ok": False, "error": str(exc)}
        else:
            checks["gateway"] = {"ok": True, "skipped": "GATEWAY_HOST is not set"}

        try:
            settings.servicenow.require_credentials()
            field_mapping = FieldMapping.load(mapping or settings.sync.mapping_file)
            snow = ServiceNowClient(settings.servicenow)
            try:
                table = field_mapping.table
                dictionary = snow.query(
                    "sys_dictionary",
                    f"name={table}^nameISNOTEMPTY",
                    fields=["name", "element", "type"],
                    limit=500,
                )
                elements = {str(item.get("element")) for item in dictionary if item.get("element")}
                missing = [
                    name
                    for name in field_mapping.fields
                    if name not in elements and not name.startswith("correlation")
                ]
                checks["servicenow"] = {
                    "ok": not missing,
                    "table": table,
                    "dictionary_fields": len(elements),
                    "missing_fields": missing,
                }
                if missing:
                    joined = ", ".join(sorted(missing))
                    problems.append(f"The ServiceNow table '{table}' has no field(s): {joined}")

                source = field_mapping.fields.get("source")
                if source and "const" in source:
                    value = str(source["const"])
                    choices = snow.query(
                        "sys_choice",
                        "name=sn_si_incident_source",
                        fields=["value", "label"],
                        limit=200,
                    )
                    values = {str(item.get("value")) for item in choices}
                    if values and value not in values:
                        problems.append(
                            f"'{value}' is not a valid sys_choice value for "
                            "sn_si_incident_source; add it on the instance first"
                        )
                        checks["servicenow"]["source_choice_ok"] = False
                    else:
                        checks["servicenow"]["source_choice_ok"] = True
            finally:
                snow.close()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"ServiceNow not usable: {exc}")
            checks["servicenow"] = {"ok": False, "error": str(exc)}

        _emit({"checks": checks, "problems": problems, "ok": not problems})
        if problems:
            typer.echo("problems found:", err=True)
            for problem in problems:
                typer.echo(f"  - {problem}", err=True)
            raise ConfigError(f"{len(problems)} problem(s) found")
        typer.echo("all checks passed")

    _run(_doctor)


def main_entry() -> int:  # pragma: no cover - console script shim
    try:
        app()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_entry())
