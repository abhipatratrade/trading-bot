"""
Structured logging with secret redaction.

Two formats:
- ``json``  — production, one JSON object per line, ingestable by Railway logs
- ``console`` — local dev, human-readable

The ``RedactingFilter`` scrubs anything that looks like an API key or HMAC
signature from log messages and `extra` payloads. It is paranoid by design:
better a false positive than a leaked credential.

Usage:
    from src.core.logging import configure_logging, get_logger
    configure_logging()
    log = get_logger(__name__)
    log.info("placed_order", symbol="BTCUSD", qty=0.1)
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from src.core.config import LogFormat, get_settings


# Patterns that should never appear in logs. Order matters — most-specific first.
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Pydantic SecretStr repr — defence in depth
    re.compile(r"SecretStr\('?\*+'?\)"),
    # Hex strings ≥ 32 chars (HMAC, hashes)
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    # Base64-looking blobs ≥ 40 chars (API tokens, JWTs)
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    # Telegram bot tokens look like "<digits>:<35+ chars>"
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
)

# Field names whose values are always redacted regardless of content.
_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "password",
        "authorization",
        "signature",
        "token",
        "access_token",
        "bot_token",
    }
)


def _redact_text(text: str) -> str:
    redacted = text
    for pattern in _REDACT_PATTERNS:
        redacted = pattern.sub("***REDACTED***", redacted)
    return redacted


def _redact_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor that redacts secrets from event dicts."""
    for key in list(event_dict.keys()):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***REDACTED***"
            continue
        value = event_dict[key]
        if isinstance(value, str):
            event_dict[key] = _redact_text(value)
    return event_dict


def configure_logging() -> None:
    """Initialise structlog. Idempotent — safe to call from every entrypoint."""
    settings = get_settings()
    level = getattr(logging, settings.log_level)

    # Stdlib root config — structlog wraps this.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == LogFormat.JSON:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Call ``configure_logging()`` first at entrypoint."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
