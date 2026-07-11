"""Indicator parity/behaviour tests (ported from backtest_engine.calc_*)."""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd

from src.shared.scanner import indicators as ind


def test_ema_first_value_and_known_series() -> None:
    s = pd.Series([1.0, 2.0, 3.0])
    e = ind.ema(s, 2)
    assert e.iloc[0] == 1.0  # adjust=False → seeds on first value
    assert math.isclose(e.iloc[1], 1.66667, rel_tol=1e-4)
    assert math.isclose(e.iloc[2], 2.55556, rel_tol=1e-4)


def test_ema_constant_series_is_constant() -> None:
    e = ind.ema(pd.Series([5.0] * 10), 4)
    assert all(abs(v - 5.0) < 1e-9 for v in e)


def test_rsi_strong_uptrend_is_high() -> None:
    # Mostly rising with one small dip so avg_loss > 0 (a pure-gains series
    # divides by zero → NaN, matching backtest_engine.calc_rsi).
    vals = [float(i) for i in range(1, 20)] + [18.5] + [float(i) for i in range(20, 30)]
    r = ind.rsi(pd.Series(vals), 14)
    assert float(r.iloc[-1]) > 90.0


def test_rsi_pure_gains_and_flat_are_nan() -> None:
    # No losses (strictly rising) OR no moves (flat) → avg_loss 0 → undefined.
    assert math.isnan(float(ind.rsi(pd.Series([float(i) for i in range(1, 30)]), 14).iloc[-1]))
    assert math.isnan(float(ind.rsi(pd.Series([100.0] * 30), 14).iloc[-1]))


def test_cci_flat_is_nan_burst_is_high() -> None:
    flat = pd.DataFrame({"high": [10.0] * 20, "low": [10.0] * 20, "close": [10.0] * 20})
    assert math.isnan(float(ind.cci(flat, 14).iloc[-1]))
    # low-vol base then a jump → large positive CCI
    closes = [10.0] * 18 + [12.0, 14.0]
    burst = pd.DataFrame({"high": closes, "low": closes, "close": closes})
    assert float(ind.cci(burst, 14).iloc[-1]) > 100


def test_supertrend_trails_below_rising_close() -> None:
    n = 40
    df = pd.DataFrame(
        {
            "high": [100.0 + i + 0.5 for i in range(n)],
            "low": [100.0 + i - 0.5 for i in range(n)],
            "close": [100.0 + i for i in range(n)],
        }
    )
    st = ind.supertrend(df, period=10, multiplier=3.0)
    last = float(st.iloc[-1])
    assert not math.isnan(last)
    assert last < float(df["close"].iloc[-1])  # uptrend → trail is support below


def test_bars_to_df_converts_decimal() -> None:
    from datetime import UTC, datetime

    from src.data_sources.base import OHLCVBar

    bar = OHLCVBar(
        timestamp=datetime(2026, 7, 1, tzinfo=UTC),
        open=Decimal("10.5"), high=Decimal("11"), low=Decimal("10"),
        close=Decimal("10.8"), volume=Decimal("1000"),
    )
    df = ind.bars_to_df([bar])
    assert df["close"].iloc[0] == 10.8
    assert df["volume"].iloc[0] == 1000.0
