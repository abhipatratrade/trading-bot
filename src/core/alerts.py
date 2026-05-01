"""
Telegram alert sender — env-gated, no-op if no token.

Usage::

    from src.core.alerts import send_alert
    send_alert("Order filled: BUY 10 BTCUSD @ 65000")
"""

from __future__ import annotations

import httpx

from src.core.config import get_settings
from src.core.logging import get_logger

_log = get_logger("core.alerts")


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
