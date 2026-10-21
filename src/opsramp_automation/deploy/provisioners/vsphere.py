"""Deploy a gateway OVA to vSphere using the ``ovftool`` binary.

``ovftool`` is used in preference to pyvmomi's ``DeployOva`` because it is
vendor neutral and considerably more reliable for the appliance images.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from ...config import VSphereSettings
from ...errors import ConfigError, GatewayError
from ...logging import get_logger
from .base import Endpoint

log = get_logger(__name__)

DEFAULT_TIMEOUT_S = 3600


class VSphereProvisioner:
    """Deploy an OVA to vCenter and wait for the appliance to answer."""

    def __init__(self, settings: VSphereSettings) -> None:
        self.settings = settings
        self.endpoint: Endpoint | None = None
        self._command: list[str] = []

    # ------------------------------------------------------------------
    def describe(self) -> str:
        return f"vSphere OVA deploy to {self.settings.host} (datastore {self.settings.datastore})"

    def build_command(self, ova_path: Path, vm_name: str) -> list[str]:
        """Assemble the ``ovftool`` command line."""
        binary = self.settings.ovftool_path
        command = [
            binary,
            "--noSSLVerify" if self.settings.insecure else "--verifySSL",
            "--acceptAllEula",
            "--diskMode=thin",
            "--powerOn",
        ]
        if self.settings.cluster:
            command.append(f"-cluster:{self.settings.cluster}")
        if self.settings.datacenter:
            command.append(f"-dc:{self.settings.datacenter}")
        command.append(f"-ds:{self.settings.datastore}")
        command.append(f"-n:{self.settings.network}")
        command.append(f"{ova_path}")
        command.append(f"vi://{self.settings.user}@{self.settings.host}/{vm_name}")
        return command

    def prepare(self, package_path: Path) -> Endpoint:
        """Deploy the OVA and return the appliance endpoint."""
        self.settings.require_connection()
        ova_path = Path(package_path)
        if not ova_path.exists():
            raise ConfigError(f"Gateway package not found: {ova_path}")
        if not shutil.which(self.settings.ovftool_path):
            raise ConfigError(
                f"'{self.settings.ovftool_path}' was not found on PATH. "
                "The vsphere provisioner requires the ovftool binary; run the job on a "
                "self-hosted runner that ships it, or use the 'existing' provisioner."
            )

        vm_name = ova_path.stem.replace("opsramp-gateway-", "opsramp-gw-")
        self._command = self.build_command(ova_path, vm_name)
        log.info("deploy.ovftool_start", command=[*self._command[:2], "..."])
        try:
            completed = subprocess.run(  # noqa: S603 - the command is fully constructed here
                self._command,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayError(f"ovftool timed out after {DEFAULT_TIMEOUT_S}s") from exc
        except OSError as exc:
            raise GatewayError(f"Unable to run ovftool: {exc}") from exc
        if completed.returncode != 0:
            raise GatewayError(
                f"ovftool failed (exit {completed.returncode}): "
                f"{(completed.stderr or completed.stdout or '').strip()[:500]}"
            )

        host = self.settings.host
        endpoint = Endpoint(host=host)
        self.endpoint = endpoint
        self._wait_for_appliance()
        return endpoint

    def _wait_for_appliance(self) -> None:
        """Poll the appliance until the admin API answers."""
        from ...config import GatewaySettings
        from ...opsramp.gateway_admin import GatewayAdminClient

        deadline = time.monotonic() + self.settings.guest_wait_s
        settings = GatewaySettings(host=self.settings.host)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            client = GatewayAdminClient(settings)
            try:
                client.get_config("generalInfo")
                log.info("deploy.appliance_ready", host=self.settings.host)
                return
            except Exception as exc:  # noqa: BLE001 - the appliance boots slowly
                last_error = exc
                time.sleep(15)
            finally:
                client.close()
        raise GatewayError(
            f"The appliance at {self.settings.host} did not answer within "
            f"{self.settings.guest_wait_s}s: {last_error}"
        )

    def teardown(self) -> None:
        """The deployed VM is intentionally left running."""
        return None
