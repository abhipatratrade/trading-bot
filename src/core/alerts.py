"""
Telegram alert sender — env-gated, no-op if no token.

Usage::

    from src.core.alerts import send_alert
    send_alert("Order filled: BUY 10 BTCUSD @ 65000")

For noisy repeating failures use ``send_alert_dedup``: it sends at most
``max_count`` pings per ``window_seconds`` for a given key, then re-arms
once the window elapses — so a transient error that recurs later is not
silently swallowed::

    send_alert_dedup(
        f"tick_error:{bucket_id}",
        f"[bot] tick error in {bucket_id}",
    )

When the underlying condition clears, call ``note_alert_recovery`` with
the same key to emit a one-off "recovered" ping and reset the counter::

    note_alert_recovery(
        f"tick_error:{bucket_id}",
        f"[bot] {bucket_id} recovered",
    )
"""

from __future__ import annotations

import time

import httpx

from src.core.config import get_settings
from src.core.logging import get_logger

_log = get_logger("core.alerts")

_DEFAULT_DEDUP_MAX = 3
_DEFAULT_DEDUP_WINDOW_SECONDS = 3600.0

# key -> (count_in_current_window, window_start_monotonic). The window is
# what makes suppression self-healing: once it elapses the key re-arms,
# so an error that stops and recurs later still pages. The old purely
# count-based counter went permanently quiet after ``max_count`` hits
# until the process restarted (journal 2026-06-23: a transient tick
# error silenced its own channel for ~6h).
_dedup_state: dict[str, tuple[int, float]] = {}


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
    key: str,
    message: str,
    max_count: int = _DEFAULT_DEDUP_MAX,
    window_seconds: float = _DEFAULT_DEDUP_WINDOW_SECONDS,
) -> bool:
    """Send an alert, capped at ``max_count`` pings per ``window_seconds``.

    Within a window, calls 1..N-1 send the bare message and the Nth
    appends a one-line "suppressed for up to Xm" notice; calls N+1 and
    beyond are dropped. Once ``window_seconds`` elapses since the window
    opened, the key re-arms automatically — so a recurring-but-transient
    error keeps paging (bounded to ``max_count`` per window) instead of
    going permanently silent until restart.
    """
    now = time.monotonic()
    count, window_start = _dedup_state.get(key, (0, now))
    if now - window_start >= window_seconds:
        count, window_start = 0, now
    count += 1
    _dedup_state[key] = (count, window_start)

    if count > max_count:
        return False
    if count == max_count:
        minutes = max(1, int(window_seconds // 60))
        message = (
            f"{message}\n(further alerts for this key suppressed for up "
            f"to {minutes}m)"
        )
    return send_alert(message)


def note_alert_recovery(key: str, message: str) -> bool:
    """Signal that ``key``'s condition has cleared.

    If the key fired at least once in the current window, send a one-off
    recovery ``message`` and reset its state so the next failure pages
    immediately. No-op (returns False) when the key was already quiet, so
    it is cheap to call on every success.
    """
    if key not in _dedup_state:
        return False
    del _dedup_state[key]
    return send_alert(message)


# key -> (first_failure_monotonic, already_alerted). Unlike the dedup
# channel (which pages on the FIRST hit), this stays silent until a failure
# has *persisted* for ``grace_seconds`` — for conditions that routinely
# self-heal within a known window (a Dhan single-session token eviction
# clears once the "one token / 2 min" cooldown passes), so a transient blip
# should not page at all. Only a genuinely stuck failure outlives the grace.
_sustained_state: dict[str, tuple[float, bool]] = {}


def note_sustained_failure(key: str, message: str, grace_seconds: float) -> bool:
    """Record a failure for ``key``; page only once it has lasted ``grace_seconds``.

    Call on every failed attempt while the condition persists. Returns True
    only on the single call that crosses the grace threshold (one page per
    episode); every earlier call within the grace window, and every call
    after the page, is a silent no-op. Clear with ``note_sustained_recovery``
    when the condition resolves so the next episode is timed afresh.
    """
    now = time.monotonic()
    first_seen, alerted = _sustained_state.get(key, (now, False))
    if alerted or now - first_seen < grace_seconds:
        _sustained_state[key] = (first_seen, alerted)
        return False
    _sustained_state[key] = (first_seen, True)
    return send_alert(message)


def note_sustained_recovery(key: str, message: str) -> bool:
    """Clear ``key``'s sustained-failure state.

    Sends a one-off recovery ``message`` only if the episode had actually
    paged (crossed the grace threshold); a blip that self-healed inside the
    grace window clears silently. Cheap to call on every success.
    """
    state = _sustained_state.pop(key, None)
    if state is not None and state[1]:
        return send_alert(message)
    return False


def reset_alert_dedup(key: str | None = None) -> None:
    """Clear one or all dedup + sustained counters. Test helper / success hook."""
    if key is None:
        _dedup_state.clear()
        _sustained_state.clear()
    else:
        _dedup_state.pop(key, None)
        _sustained_state.pop(key, None)
