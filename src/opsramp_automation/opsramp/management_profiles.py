"""OpsRamp Cloud API: management profiles, gateway packages and tunnel control."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx

from ..errors import OpsRampError, OpsRampNotFoundError
from ..logging import get_logger
from .client import OpsRampClient
from .models import DownloadPackage, ManagementProfile, ManagementProfileSearchResult

log = get_logger(__name__)

PACKAGE_NAMES = ("ova", "iso", "windows", "google", "nextgen_iso", "nextgen_ova")


class ManagementProfilesApi:
    """CRUD, attach/detach and package download for management profiles."""

    def __init__(self, client: OpsRampClient) -> None:
        self.client = client

    @property
    def _base(self) -> str:
        return f"/v2/tenants/{self.client.settings.tenant_id}/managementProfiles"

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------
    def list_profiles(
        self, query: str | None = None, page_size: int = 100
    ) -> list[ManagementProfile]:
        """Search management profiles.

        ``query`` follows the OpsRamp ``key:value`` filter syntax, e.g.
        ``"name:Gateway_Profile"`` or ``"outOfSync:true"``.
        """
        params: dict[str, Any] = {"pageSize": page_size}
        if query:
            params["queryString"] = query
        payload = self.client.get(f"{self._base}/search", params=params)
        if not isinstance(payload, dict):
            return []
        parsed = ManagementProfileSearchResult.model_validate(payload)
        return list(parsed.results)

    def find_by_name(self, name: str) -> ManagementProfile | None:
        """Return the first profile with an exact name match, or ``None``."""
        for profile in self.list_profiles():
            if profile.name == name:
                return profile
        return None

    def get_profile(self, profile_id: int | str) -> ManagementProfile:
        """Fetch a single management profile."""
        payload = self.client.get(f"{self._base}/{profile_id}")
        if not isinstance(payload, dict):
            raise OpsRampNotFoundError(f"Management profile {profile_id} returned no data")
        return ManagementProfile.model_validate(payload)

    def find_out_of_sync(self, uuid: str | None = None) -> list[ManagementProfile]:
        """List profiles whose deployed gateway configuration drifted."""
        query = "outOfSync:true"
        if uuid:
            query = f"outOfSync:true+uuid:{uuid}"
        return self.list_profiles(query)

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------
    def create_profile(
        self,
        name: str,
        description: str = "",
        profile_type: str = "Gateway",
    ) -> ManagementProfile:
        """Create a management profile; the response carries the activation token."""
        payload = self.client.post(
            self._base,
            json_body={"name": name, "description": description, "type": profile_type},
        )
        if not isinstance(payload, dict):
            raise OpsRampError("Creating the management profile returned no data")
        return ManagementProfile.model_validate(payload)

    def attach(self, profile_id: int | str) -> ManagementProfile:
        """Attach a gateway to the profile and (re)generate the activation token."""
        payload = self.client.get(f"{self._base}/{profile_id}/attach")
        if not isinstance(payload, dict):
            raise OpsRampError(f"Attaching management profile {profile_id} returned no data")
        return ManagementProfile.model_validate(payload)

    def detach(self, profile_id: int | str) -> dict[str, Any]:
        """Detach the gateway from the profile, invalidating the activation token."""
        payload = self.client.get(f"{self._base}/{profile_id}/detach")
        return payload if isinstance(payload, dict) else {}

    def reconnect_tunnel(self, profile_id: int | str) -> dict[str, Any]:
        """Ask OpsRamp to re-establish the gateway tunnel."""
        payload = self.client.get(f"{self._base}/{profile_id}/reconnectTunnel")
        return payload if isinstance(payload, dict) else {}

    def set_services(
        self,
        profile_id: int | str,
        services: dict[str, bool],
        name: str | None = None,
        profile_type: str = "Gateway",
    ) -> ManagementProfile:
        """Enable or disable the profile's agents (e.g. ``sipAgent``)."""
        body = {
            "name": name or "",
            "type": profile_type,
            "services": [
                {"name": key, "status": "true" if value else "false"}
                for key, value in services.items()
            ],
        }
        payload = self.client.post(f"{self._base}/{profile_id}", json_body=body)
        if not isinstance(payload, dict):
            raise OpsRampError(f"Updating services for {profile_id} returned no data")
        return ManagementProfile.model_validate(payload)

    # ------------------------------------------------------------------
    # package download
    # ------------------------------------------------------------------
    def get_download_url(self, package_name: str) -> DownloadPackage:
        """Return the signed download URL for a gateway package."""
        if package_name not in PACKAGE_NAMES:
            expected = ", ".join(PACKAGE_NAMES)
            raise OpsRampError(
                f"Unknown gateway package '{package_name}'; expected one of {expected}"
            )
        payload = self.client.get(f"/v2/download/gateway/{package_name}")
        if not isinstance(payload, dict) or not payload.get("url"):
            raise OpsRampError(f"No download URL returned for gateway package '{package_name}'")
        return DownloadPackage.model_validate(payload)

    def download_package(
        self,
        package_name: str,
        target_dir: Path,
        *,
        timeout_s: float = 300.0,
    ) -> Path:
        """Stream the gateway package to disk and return the local path."""
        descriptor = self.get_download_url(package_name)
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / f"opsramp-gateway-{package_name}"
        digest = hashlib.sha256()
        try:
            with httpx.stream(
                "GET",
                descriptor.url,
                timeout=httpx.Timeout(timeout_s, connect=30.0),
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                        digest.update(chunk)
        except httpx.HTTPError as exc:
            raise OpsRampError(f"Failed to download the gateway package: {exc}") from exc
        log.info(
            "opsramp.package_downloaded",
            path=str(destination),
            sha256=digest.hexdigest(),
            bytes=destination.stat().st_size,
        )
        return destination
