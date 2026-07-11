"""NSE market-hours gate — session classification (Phase 4/M6)."""

from __future__ import annotations

from datetime import datetime

from src.shared.market_calendar import IST, NseSession, is_trading_day, nse_session


def _ist(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# 2026-07-13 is a Monday (a normal trading day).
def test_entry_window() -> None:
    assert nse_session(_ist(2026, 7, 13, 9, 50)) is NseSession.ENTRY_WINDOW
    assert nse_session(_ist(2026, 7, 13, 9, 45)) is NseSession.ENTRY_WINDOW  # start
    assert nse_session(_ist(2026, 7, 13, 10, 30)) is NseSession.ENTRY_WINDOW  # end


def test_open_but_no_entry() -> None:
    assert nse_session(_ist(2026, 7, 13, 9, 15)) is NseSession.OPEN_NO_ENTRY  # open
    assert nse_session(_ist(2026, 7, 13, 11, 0)) is NseSession.OPEN_NO_ENTRY
    assert nse_session(_ist(2026, 7, 13, 15, 30)) is NseSession.OPEN_NO_ENTRY  # close


def test_closed_outside_hours() -> None:
    assert nse_session(_ist(2026, 7, 13, 8, 0)) is NseSession.CLOSED   # pre-open
    assert nse_session(_ist(2026, 7, 13, 16, 0)) is NseSession.CLOSED  # post-close


def test_closed_weekend() -> None:
    # 2026-07-18 is a Saturday.
    assert nse_session(_ist(2026, 7, 18, 10, 0)) is NseSession.CLOSED


def test_closed_holiday() -> None:
    # 2026-01-26 Republic Day (a Monday) — a listed holiday.
    assert not is_trading_day(_ist(2026, 1, 26, 10, 0).date())
    assert nse_session(_ist(2026, 1, 26, 10, 0)) is NseSession.CLOSED


def test_utc_input_is_converted() -> None:
    # 04:20 UTC == 09:50 IST → entry window.
    assert nse_session(datetime(2026, 7, 13, 4, 20)) is NseSession.ENTRY_WINDOW
