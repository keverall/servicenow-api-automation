"""Structured JSON logging with secret redaction.

Every log record is emitted as a single JSON object on stdout so that GitHub
Actions can surface it without a parser.  Values whose *key* looks secret are
replaced before serialisation, which covers request bodies, headers and
connection settings.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "***REDACTED***"

#: Keys whose values must never reach the log stream.
REDACT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "oldpasswd",
        "newpasswd",
        "proxy_pwd",
        "proxy_password",
        "client_secret",
        "clientsecret",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "vtoken",
        "api_key",
        "apikey",
        "apiSecret",
        "api_secret",
        "authorization",
        "auth",
        "x-api-key",
        "private_key",
        "activation_token",
        "activationToken",
    }
)

#: Keys that hold a header mapping whose individual entries must be inspected.
_HEADER_KEYS: frozenset[str] = frozenset({"headers", "header"})


def _redact_value(key: str) -> bool:
    lowered = key.lower()
    return lowered in {k.lower() for k in REDACT_KEYS}


def redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Recursively replace secret-looking values with a placeholder."""
    return _redact_mapping(dict(event_dict))


def _redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _redact_value(key):
            out[key] = REDACTED
        elif (key.lower() in _HEADER_KEYS and isinstance(value, dict)) or isinstance(value, dict):
            out[key] = _redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [_redact_mapping(item) if isinstance(item, dict) else item for item in value]
        else:
            out[key] = value
    return out


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog + stdlib logging.

    ``json_output=False`` switches to the human friendly console renderer, which
    is convenient when running the CLI locally.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: Any = structlog.processors.JSONRenderer()
    if not json_output:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
