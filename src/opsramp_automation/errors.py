"""Typed exceptions and the process exit-code mapping used by the CLI."""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_RATE_LIMIT = 4
EXIT_GATEWAY = 5
EXIT_SYNC = 6


class OpsRampError(Exception):
    """Base class for every error raised by this package."""

    exit_code: int = EXIT_GENERIC

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigError(OpsRampError):
    """Invalid or missing configuration."""

    exit_code = EXIT_CONFIG


class OpsRampAuthError(OpsRampError):
    """OpsRamp rejected the supplied credentials or the token expired irrecoverably."""

    exit_code = EXIT_AUTH


class OpsRampRateLimitError(OpsRampError):
    """OpsRamp rate limit was exhausted and could not be satisfied within the cap."""

    exit_code = EXIT_RATE_LIMIT


class OpsRampNotFoundError(OpsRampError):
    """The requested OpsRamp resource does not exist."""

    exit_code = EXIT_GENERIC


class OpsRampApiError(OpsRampError):
    """OpsRamp returned an unexpected error response."""

    exit_code = EXIT_GENERIC


class GatewayError(OpsRampError):
    """The gateway appliance admin API returned an error or is unreachable."""

    exit_code = EXIT_GATEWAY


class GatewayAuthError(GatewayError):
    """The gateway appliance rejected the admin credentials."""

    exit_code = EXIT_AUTH


class ServiceNowError(OpsRampError):
    """ServiceNow returned an unexpected error response."""

    exit_code = EXIT_GENERIC


class ServiceNowAuthError(ServiceNowError):
    """ServiceNow rejected the supplied credentials."""

    exit_code = EXIT_AUTH


class SyncError(OpsRampError):
    """The alert to ServiceNow sync failed."""

    exit_code = EXIT_SYNC


def exit_code_for(exc: BaseException) -> int:
    """Map any exception to the process exit code the CLI should return."""
    if isinstance(exc, OpsRampError):
        return exc.exit_code
    return EXIT_GENERIC
