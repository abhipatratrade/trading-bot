"""TF-scaled dedup window (Phase 1c — replaces hardcoded 23h)."""

from __future__ import annotations

import pytest

from src.shared.allocator.sizer import dedup_window_hours_for_tf


def test_daily_tf_matches_legacy_23h() -> None:
    assert dedup_window_hours_for_tf("1d") == 23.0


def test_hourly_tf() -> None:
    assert dedup_window_hours_for_tf("1h") == pytest.approx(23 / 24)


def test_four_hour_tf() -> None:
    assert dedup_window_hours_for_tf("4h") == pytest.approx(4 * 23 / 24)


def test_five_minute_tf() -> None:
    assert dedup_window_hours_for_tf("5m") == pytest.approx(5 / 60 * 23 / 24)


def test_garbage_falls_back_to_23h() -> None:
    assert dedup_window_hours_for_tf("") == 23.0
    assert dedup_window_hours_for_tf("daily") == 23.0


# ── Underlying-keyed dedup (Decision 036) ───────────────────────────────
def test_dedup_key_is_identity_for_cash_and_crypto() -> None:
    """Every pre-036 bucket must be unaffected by the F&O change."""
    from src.shared.allocator.sizer import dedup_keys

    assert dedup_keys({"SWIGGY", "NAM-INDIA", "BTCUSD"}) == {
        "SWIGGY", "NAM-INDIA", "BTCUSD",
    }


def test_dedup_key_collapses_every_strike_of_one_underlying() -> None:
    """The gate the sizer relies on: the ledger holds contract symbols, the
    scanner offers underlyings, and a gate comparing the two would never match
    — so a strategy already short one NIFTY strike would open a second."""
    from src.shared.allocator.sizer import dedup_keys

    held = {
        "NIFTY-20260908-23150-CE",
        "NIFTY-20260915-23200-PE",
        "NIFTY-20260929-FUT",
        "SBIN-20260929-FUT",
    }
    keys = dedup_keys(held)
    assert keys == {"NIFTY", "SBIN"}
    # A fresh NIFTY signal from the scanner is correctly gated out.
    assert "NIFTY" in keys
