"""
Telegram alert sender — env-gated, no-op if no token.

Usage::

    from src.core.alerts import send_alert
    send_alert("Order filled: BUY 10 BTCUSD @ 65000")

For noisy repeating failures use ``send_alert_dedup`` so the same
(key, message) pair stops pinging after a few hits::

    send_alert_dedup(
        f"tick_error:{bucket_id}",
        f"[bot] tick error in {bucket_id}",
    )
"""

from __future__ import annotations

import httpx

from src.core.config import get_settings
from src.core.logging import get_logger

_log = get_logger("core.alerts")

_DEFAULT_DEDUP_MAX = 3
_dedup_counters: dict[str, int] = {}


def send_alert(message: str) -> bool:
    """Send a Telegram message.  Returns True on success, False if disabled or failed."""
    settings = get_settings()
    if not settings.telegram_enabled:
        return False

    token = settings.telegram_bot_token.get_secret_value()  # type: ignore[union-attr]
    chat_id = settings.telegram_chat_id
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            _log.warning("telegram_send_failed", status=resp.status_code)
            return False
        return True
    except Exception:
        _log.warning("telegram_send_error", exc_info=True)
        return False


def send_alert_dedup(
    key: str, message: str, max_count: int = _DEFAULT_DEDUP_MAX
) -> bool:
    """Send an alert but suppress further pings once ``key`` hits ``max_count``.

    The counter lives in-process and resets only on restart. Calls 1..N-1
    send the bare message; the Nth (default 3rd) appends a one-line
    "further alerts suppressed" notice so the user knows the channel for
    that key is going quiet. Calls N+1 and beyond are dropped silently.
    """
    count = _dedup_counters.get(key, 0) + 1
    _dedup_counters[key] = count

    if count > max_count:
        return False
    if count == max_count:
        message = (
            f"{message}\n(further alerts for this key suppressed until restart)"
        )
    return send_alert(message)


def reset_alert_dedup(key: str | None = None) -> None:
    """Clear one or all dedup counters. Test helper."""
    if key is None:
        _dedup_counters.clear()
    else:
        _dedup_counters.pop(key, None)
