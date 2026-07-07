"""Per-bucket tick cadence from bucket timeframe (Phase 1c)."""

from __future__ import annotations

from src.shared.bucket_runner import tick_interval_for_tf


def test_fast_tfs_stay_at_60s() -> None:
    assert tick_interval_for_tf("1m") == 60
    assert tick_interval_for_tf("5m") == 60
    assert tick_interval_for_tf("15m") == 60


def test_mid_tfs_scale() -> None:
    assert tick_interval_for_tf("1h") == 180
    assert tick_interval_for_tf("4h") == 720


def test_slow_tfs_cap_at_15_min() -> None:
    assert tick_interval_for_tf("1d") == 900
    assert tick_interval_for_tf("1w") == 900


def test_case_insensitive_unit() -> None:
    assert tick_interval_for_tf("1D") == 900


def test_garbage_falls_back_to_60s() -> None:
    assert tick_interval_for_tf("") == 60
    assert tick_interval_for_tf("daily") == 60
    assert tick_interval_for_tf("h1") == 60
