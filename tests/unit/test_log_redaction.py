"""Secret redaction — structlog AND stdlib paths.

Regression for 2026-07-21: httpx logged the live Telegram bot token in
plaintext to the systemd journal on every alert, because redaction was a
structlog processor and httpx logs through stdlib.
"""

from __future__ import annotations

import logging

from src.core.logging import RedactingFilter, _redact_processor, _redact_text

# Shaped like a real Telegram token (digits ":" 35 chars) but not a real one.
_FAKE_TOKEN = "1234567890:AAFakeFakeFakeFakeFakeFakeFakeFake1"
_TG_URL = f"https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage"


def _apply(record: logging.LogRecord) -> str:
    assert RedactingFilter().filter(record) is True, "must never drop records"
    return record.getMessage()


def test_httpx_style_record_is_redacted() -> None:
    """The exact shape that leaked: URL as a %s ARG, not in the message."""
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", _TG_URL, "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )
    out = _apply(record)
    assert _FAKE_TOKEN not in out
    assert "REDACTED" in out
    assert "api.telegram.org" in out, "only the secret should go, not the context"


def test_non_string_arg_type_is_preserved_when_clean() -> None:
    """Redaction must not stringify unrelated args (e.g. an httpx.URL)."""

    class _Url:
        def __str__(self) -> str:
            return "https://api.dhan.co/v2/orders"

    url = _Url()
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg="HTTP Request: %s %s", args=("POST", url), exc_info=None,
    )
    RedactingFilter().filter(record)
    assert record.args[1] is url, "clean args must keep their original type"


def test_secret_in_message_body_is_redacted() -> None:
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"calling {_TG_URL}", args=None, exc_info=None,
    )
    assert _FAKE_TOKEN not in _apply(record)


def test_dict_args_are_redacted() -> None:
    # logger.info("%(url)s", {...}) reaches LogRecord as a 1-tuple holding the
    # mapping, which LogRecord then unwraps — passing the bare dict raises.
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%(url)s", args=({"url": _TG_URL},), exc_info=None,
    )
    assert isinstance(record.args, dict), "LogRecord should have unwrapped it"
    assert _FAKE_TOKEN not in _apply(record)


def test_structlog_path_still_redacts() -> None:
    out = _redact_processor(None, "info", {"event": "alert", "url": _TG_URL})
    assert _FAKE_TOKEN not in out["url"]


def test_hex_and_base64_secrets_still_covered() -> None:
    assert "deadbeef" * 4 not in _redact_text("sig=" + "deadbeef" * 4)
    assert _redact_text("plain text is untouched") == "plain text is untouched"


def test_telegram_token_redacted_in_every_url_shape() -> None:
    """The token must not depend on a long trailing path to get caught.

    The original pattern was anchored with a leading \b, which never matches
    in ".../bot<digits>" — 't' and the first digit are both word characters.
    Real URLs were only scrubbed incidentally, by the base64 rule, and a short
    path (/getMe) or no path leaked the token in full.
    """
    for path in ("/sendMessage", "/getMe", ""):
        url = f"https://api.telegram.org/bot{_FAKE_TOKEN}{path}"
        assert _FAKE_TOKEN not in _redact_text(url), f"leaked with path={path!r}"


def test_bare_token_and_secret_half_both_redacted() -> None:
    assert _FAKE_TOKEN not in _redact_text(_FAKE_TOKEN)
    secret_half = _FAKE_TOKEN.split(":", 1)[1]
    assert secret_half not in _redact_text(f"token={_FAKE_TOKEN}")


def test_ordinary_colon_text_is_not_over_redacted() -> None:
    """Guard the loosened pattern against obvious false positives."""
    for benign in (
        "2026-07-21T19:39:59.615876Z",
        "elapsed 12:34",
        "bucket_id=intraday-indian symbol=RELIANCE qty=100",
    ):
        assert _redact_text(benign) == benign, benign
