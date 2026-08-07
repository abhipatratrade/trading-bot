"""
Structured logging with secret redaction.

Two formats:
- ``json``  — production, one JSON object per line, ingestable by Railway logs
- ``console`` — local dev, human-readable

Redaction runs on BOTH logging paths, which is the whole point:
- ``_redact_processor`` — structlog calls made by this codebase.
- ``RedactingFilter``   — stdlib records from third-party libraries.

The second one is not optional. httpx logs every request URL at INFO through
stdlib, so it never reaches structlog's processors; on 2026-07-21 that put the
live Telegram bot token into the systemd journal in plaintext on every alert.
Both are paranoid by design: better a false positive than a leaked credential.

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

# Query-string parameters whose VALUE is a credential. Matched by NAME, because
# these secrets have no distinguishing shape: the Dhan login PIN and a TOTP code
# are both six digits, and any value-shaped rule wide enough to catch them would
# also redact quantities, prices and security ids.
#
# This is the gap that leaked the live Dhan PIN. Every rule below this comment
# block is shape-based, so
#   POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=…&pin=…&totp=…
# passed all of them untouched and httpx wrote it to the journal at INFO on
# EVERY mint attempt — ~3,800 times a day during the 2026-08-04/05 token
# outage. Found 2026-08-07; the PIN was rotated.
_SECRET_QUERY_KEYS = (
    "pin",
    "totp",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "accesstoken",
    "api_key",
    "apikey",
    "auth",
)

# (pattern, replacement) — a per-pattern replacement so a named rule can keep
# the key and scrub only the value, which leaves the log line readable.
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Sensitive query params, in a URL or a stringified dict/params mapping.
    (
        re.compile(
            r"(?i)\b(" + "|".join(_SECRET_QUERY_KEYS) + r")=[^&\s\"'>)}\],]+"
        ),
        r"\1=***REDACTED***",
    ),
    # Pydantic SecretStr repr — defence in depth
    (re.compile(r"SecretStr\('?\*+'?\)"), "***REDACTED***"),
    # Hex strings ≥ 32 chars (HMAC, hashes)
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "***REDACTED***"),
    # Base64-looking blobs ≥ 40 chars (API tokens, JWTs)
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "***REDACTED***"),
    # Telegram bot tokens look like "<digits>:<35+ chars>".
    # NO leading \b on purpose: these appear in URLs as ".../bot<token>", and
    # "bot1234..." has no word boundary between the 't' and the digits, so an
    # anchored pattern silently missed every real Telegram URL. Verified
    # 2026-07-21 — the token only got scrubbed by luck, because the base64 rule
    # below caught "<secret>/sendMessage" once the path pushed it past 40
    # chars; a short path like /getMe, or no path, leaked in full.
    (re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"), "***REDACTED***"),
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
        "pin",
        "totp",
        "totp_secret",
    }
)


def _redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _REDACT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
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


def _redact_arg(arg: object) -> object:
    """Redact one stdlib log arg, preserving its type unless it holds a secret.

    Third-party libraries pass rich objects, not strings — httpx logs the URL
    as an ``httpx.URL``. Stringifying everything would change how unrelated
    records render, so we only substitute when redaction actually fires.
    """
    if isinstance(arg, str):
        return _redact_text(arg)
    text = str(arg)
    redacted = _redact_text(text)
    return redacted if redacted != text else arg


class RedactingFilter(logging.Filter):
    """Applies the same redaction to STDLIB log records.

    ``_redact_processor`` only covers structlog calls. Anything logging through
    stdlib — httpx, urllib3, sqlalchemy, any dependency — bypassed it entirely
    and printed raw.

    That was not theoretical: on 2026-07-21 httpx logged
    ``HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage`` at
    INFO on every alert, putting the live bot token in the systemd journal in
    plaintext. Note the URL arrives as a ``%s`` ARG rather than in ``msg``, so
    filtering the message alone would not have caught it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact_arg(v) for k, v in record.args.items()
                }
            else:
                record.args = tuple(_redact_arg(a) for a in record.args)
        return True  # never drop a record, only scrub it


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

    # Scrub stdlib records too (httpx et al. never touch structlog's
    # processors). Attached to the HANDLERS rather than the root logger:
    # a filter on a logger does not apply to records propagated up from
    # child loggers, so a logger-level filter would miss "httpx" entirely.
    _redactor = RedactingFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(_redactor)

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
