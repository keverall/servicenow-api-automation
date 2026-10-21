"""Provisioner for an appliance that is already running.

This is the default because it needs nothing beyond the admin API: the
deployment verifies the appliance is reachable and reports a clear error when
it is not.
"""

from __future__ import annotations

from pathlib import Path

from ...errors import GatewayError
from ...logging import get_logger
from .base import Endpoint

log = get_logger(__name__)


class ExistingGatewayProvisioner:
    """Verify that the appliance at ``host`` is up and reachable."""

    def __init__(self, host: str, port: int = 5480) -> None:
        self.host = host
        self.port = port
        self.endpoint: Endpoint | None = None

    def prepare(self, package_path: Path | None = None) -> Endpoint:
        """Probe the admin API and return the endpoint."""
        if not self.host:
            raise GatewayError(
                "GATEWAY_HOST is required: the existing provisioner does not deploy the appliance"
            )
        endpoint = Endpoint(host=self.host, port=self.port)
        from ...config import GatewaySettings
        from ...opsramp.gateway_admin import GatewayAdminClient

        settings = GatewaySettings(host=self.host, port=self.port)
        client = GatewayAdminClient(settings)
        try:
            client.get_config("generalInfo")
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(
                f"Cannot reach the gateway appliance at {endpoint.base_url}: {exc}. "
                "Check the host, the firewall and the (self-signed) certificate."
            ) from exc
        finally:
            client.close()
        self.endpoint = endpoint
        log.info("deploy.provisioned", provisioner="existing", host=endpoint.host)
        return endpoint

    def teardown(self) -> None:
        """Nothing to release: the appliance was not created by us."""
        return None

    def describe(self) -> str:
        return f"existing gateway at {self.host}:{self.port}"
