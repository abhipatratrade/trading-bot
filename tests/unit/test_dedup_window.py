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
