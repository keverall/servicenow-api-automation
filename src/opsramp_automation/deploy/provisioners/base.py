"""The provisioner protocol.

A provisioner turns a downloaded gateway package into a reachable appliance
endpoint.  Two implementations ship: ``existing`` (the appliance is already
running) and ``vsphere`` (deploy an OVA with ``ovftool``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Endpoint:
    """Where a provisioned gateway can be reached."""

    host: str
    port: int = 5480

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"


@runtime_checkable
class Provisioner(Protocol):
    """Makes a gateway package available as a running appliance."""

    def prepare(self, package_path: Path) -> Endpoint:
        """Provision (or verify) the appliance and return its endpoint."""
        ...

    def teardown(self) -> None:
        """Release any resources held by the provisioner."""
        ...

    def describe(self) -> str:
        """A short, log-safe description of the provisioner."""
        ...
