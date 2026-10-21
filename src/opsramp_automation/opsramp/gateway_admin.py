"""Client for the Classic OpsRamp gateway appliance admin API (``https://<gw>:5480``).

This is a *different* API surface from the OpsRamp Cloud API: it uses the
appliance admin credentials and the ``Authorization: token <token>`` scheme
(not ``Bearer``), and the appliance ships with a self-signed certificate.
"""

from __future__ import annotations

import ssl
import time
from typing import Any

import httpx

from ..config import GatewaySettings
from ..errors import GatewayAuthError, GatewayError
from ..http import RetryPolicy, build_client, request_with_retry
from ..logging import get_logger

log = get_logger(__name__)

INFO_TYPES = ("generalInfo", "syslogInfo", "ntpInfo", "tzInfo", "servicesInfo", "routesInfo")
MONITORABLE_SERVICES = ("network", "squid", "postgresql", "ntp")
SERVICE_ACTIONS = ("start", "stop", "status", "restart")


class GatewayAdminClient:
    """Administrative operations against a deployed Classic gateway."""

    def __init__(
        self,
        settings: GatewaySettings,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = http_client is None
        verify: bool | str | ssl.SSLContext = settings.verify_tls
        if settings.ca_bundle:
            verify = ssl.create_default_context(cafile=str(settings.ca_bundle))
        elif not settings.verify_tls:
            log.warning(
                "gateway.tls_verification_disabled",
                host=settings.host,
                hint="Provide GATEWAY_CA_BUNDLE with the appliance CA certificate instead",
            )
        self._http = http_client or build_client(
            verify=verify,
            timeout=settings.timeout_s,
        )
        self.policy = RetryPolicy(max_retries=settings.max_retries)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def base_url(self) -> str:
        return f"https://{self.settings.host}:{self.settings.port}"

    # ------------------------------------------------------------------
    # authentication
    # ------------------------------------------------------------------
    def authenticate(self) -> str:
        """Obtain an admin token, reusing the cached one while it is valid."""
        if self._token and time.time() < self._token_expires_at:
            return self._token
        response = self._request(
            "POST",
            "/v2/auth/token",
            json_body={
                "username": self.settings.username,
                "password": self.settings.password.get_secret_value(),
            },
            authenticated=False,
        )
        if response.status_code in (401, 403):
            raise GatewayAuthError(
                f"Gateway rejected the admin credentials for user '{self.settings.username}'"
            )
        if response.status_code >= 400:
            raise GatewayError(
                f"Gateway authentication failed (HTTP {response.status_code}): {_snippet(response)}"
            )
        payload = response.json()
        token = payload.get("token")
        if not token:
            raise GatewayError("Gateway auth response did not contain a token")
        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._token = str(token)
        self._token_expires_at = time.time() + max(0.0, expires_in)
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.authenticate()}",
            "Accept": "application/json",
        }

    def _refresh_auth(self) -> dict[str, str]:
        self._token = None
        self._token_expires_at = 0.0
        return self._auth_headers()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers() if authenticated else {"Accept": "application/json"}
        try:
            return request_with_retry(
                self._http,
                method,
                url,
                policy=self.policy,
                headers=headers,
                refresh_auth=self._refresh_auth if authenticated else None,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise GatewayError(
                f"Cannot reach the gateway appliance at {self.base_url}: {exc}. "
                "Check the host, the firewall and the (self-signed) certificate."
            ) from exc

    # ------------------------------------------------------------------
    # configuration inspection
    # ------------------------------------------------------------------
    def get_config(self, info: str = "generalInfo") -> dict[str, Any]:
        """Return the requested configuration block."""
        if info not in INFO_TYPES:
            raise GatewayError(
                f"Unknown gateway config '{info}'; expected one of {', '.join(INFO_TYPES)}"
            )
        response = self._request("GET", "/v2/gateway", params={"info": info})
        if response.status_code in (401, 403):
            raise GatewayAuthError("Gateway rejected the admin token")
        if response.status_code >= 400:
            raise GatewayError(
                f"Gateway returned HTTP {response.status_code} reading '{info}': "
                f"{_snippet(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(f"Gateway returned a non-JSON response for '{info}'") from exc
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def action(self, action_name: str, body: dict[str, Any] | None = None) -> str:
        """Invoke ``POST /v2/gateway/actions/{action_name}`` and return the message."""
        response = self._request("POST", f"/v2/gateway/actions/{action_name}", json_body=body)
        if response.status_code in (401, 403):
            raise GatewayAuthError("Gateway rejected the admin token")
        if response.status_code >= 400:
            raise GatewayError(
                f"Gateway action '{action_name}' failed (HTTP {response.status_code}): "
                f"{_snippet(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(f"Gateway action '{action_name}' returned a non-JSON body") from exc
        if not isinstance(payload, dict) or "success" not in payload:
            raise GatewayError(
                f"Gateway action '{action_name}' did not report success: {_snippet(response)}"
            )
        message = str(payload.get("success") or "")
        log.info("gateway.action", action=action_name, message=message)
        return message

    def register(self, server: str, activation_token: str) -> str:
        """Register the appliance with the OpsRamp cloud."""
        if not server:
            raise GatewayError(
                "The OpsRamp API server URL is required for registration. "
                "Set GATEWAY_REGISTRATION_SERVER (shown next to the activation token in the UI)."
            )
        return self.action(
            "registration",
            {"registration": {"server": server, "activation_token": activation_token}},
        )

    def set_network_settings(
        self,
        *,
        domainname: str,
        hostname: str,
        interface: str,
        dhcp: bool | str,
        ipaddr: str,
        subnet: str,
        gateway: str,
        dnsArray: str | list[str],
    ) -> str:
        dns = dnsArray if isinstance(dnsArray, str) else ",".join(dnsArray)
        return self.action(
            "networkSettings",
            {
                "networkSettings": {
                    "domainname": domainname,
                    "hostname": hostname,
                    "interface": interface,
                    "dhcp": ("True" if dhcp else "False") if isinstance(dhcp, bool) else str(dhcp),
                    "ipaddr": ipaddr,
                    "subnet": subnet,
                    "gateway": gateway,
                    "dnsArray": dns,
                }
            },
        )

    def add_static_route(
        self,
        *,
        srouteIP: str,
        srouteGateway: str,
        srouteSubnet: str,
        srouteMetric: str | int,
        srouteInterface: str,
    ) -> str:
        return self.action(
            "staticRoutes",
            {
                "staticRoutes": {
                    "srouteIP": srouteIP,
                    "srouteGateway": srouteGateway,
                    "srouteSubnet": srouteSubnet,
                    "srouteMetric": str(srouteMetric),
                    "srouteInterface": srouteInterface,
                }
            },
        )

    def set_ntp_servers(self, servers: list[str]) -> str:
        return self.action("ntpUpdate", {"ntpUpdate": {"ntpServers": list(servers)}})

    def ntp_sync(self) -> str:
        return self.action("ntpSync")

    def add_syslog(
        self,
        *,
        receiver: str,
        port: str | int,
        protocol: str,
        selectors: str,
    ) -> str:
        return self.action(
            "syslogAdd",
            {
                "syslogInfo": {
                    "selectors": selectors,
                    "protocol": protocol,
                    "port": str(port),
                    "receiver": receiver,
                }
            },
        )

    def update_syslog(
        self,
        *,
        receiver: str,
        port: str | int,
        protocol: str,
        selectors: str,
    ) -> str:
        return self.action(
            "syslogUpdate",
            {
                "syslogInfo": {
                    "selectors": selectors,
                    "protocol": protocol,
                    "port": str(port),
                    "receiver": receiver,
                }
            },
        )

    def delete_syslog(
        self,
        *,
        receiver: str,
        port: str | int,
        protocol: str,
        selectors: str,
    ) -> str:
        return self.action(
            "syslogDelete",
            {
                "syslogInfo": {
                    "selectors": selectors,
                    "protocol": protocol,
                    "port": str(port),
                    "receiver": receiver,
                }
            },
        )

    def reset_syslog(self) -> str:
        return self.action("syslogReset")

    def set_timezone(self, tzs: str) -> str:
        return self.action("timeZone", {"timeZone": {"tzs": tzs}})

    def set_datetime(self, *, hour: str | int, min: str | int, sec: str | int, date: str) -> str:
        return self.action(
            "dateTime",
            {
                "dateTime": {
                    "hour": str(hour),
                    "min": str(min),
                    "sec": str(sec),
                    "date": date,
                }
            },
        )

    def set_proxy(
        self,
        *,
        connection: str,
        proxy_ip: str = "",
        proxy_port: str = "",
        proxy_usr: str = "",
        proxy_pwd: str = "",
    ) -> str:
        if connection not in ("proxy", "direct"):
            raise GatewayError("Proxy connection must be either 'proxy' or 'direct'")
        return self.action(
            "externalProxy",
            {
                "externalProxy": {
                    "connection": connection,
                    "proxy_ip": proxy_ip,
                    "proxy_port": proxy_port,
                    "proxy_usr": proxy_usr,
                    "proxy_pwd": proxy_pwd,
                }
            },
        )

    def change_password(self, *, username: str, oldpasswd: str, newpasswd: str) -> str:
        return self.action(
            "passwdChange",
            {
                "passwdChange": {
                    "username": username,
                    "oldpasswd": oldpasswd,
                    "newpasswd": newpasswd,
                }
            },
        )

    def monit_service(self, service: str, action: str) -> str:
        if service not in MONITORABLE_SERVICES:
            raise GatewayError(
                f"Unknown service '{service}'; expected one of {', '.join(MONITORABLE_SERVICES)}"
            )
        if action not in SERVICE_ACTIONS:
            raise GatewayError(
                f"Unknown service action '{action}'; expected one of {', '.join(SERVICE_ACTIONS)}"
            )
        return self.action("monitService", {"monitService": {"service": service, "action": action}})

    def restart_all_services(self) -> str:
        return self.action("restartAllServices")

    # ------------------------------------------------------------------
    # convenience helpers
    # ------------------------------------------------------------------
    def registration_status(self) -> str:
        """Return ``generalInfo.applianceInformation.registrationStatus``."""
        config = self.get_config("generalInfo")
        general = config.get("generalInfo") or {}
        appliance = general.get("applianceInformation") or {}
        return str(appliance.get("registrationStatus") or "")

    def tunnel_status(self) -> str:
        """Return ``generalInfo.applianceInformation.tunnelStatus``."""
        config = self.get_config("generalInfo")
        general = config.get("generalInfo") or {}
        appliance = general.get("applianceInformation") or {}
        return str(appliance.get("tunnelStatus") or "")

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> GatewayAdminClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - defensive: never fail on an unreadable body
        return "<unreadable body>"
    return " ".join(text.split())[:limit]
