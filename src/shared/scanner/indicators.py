"""
Technical indicators — ported byte-for-byte from the Backtesting Engine
(``backtest_engine.calc_*``) so the live bucket's numbers equal the backtested
ones (the backtester is a separate repo — Decision "Backtest engine out of
scope" — hence a vendored copy, not an import).

Pure pandas/numpy; no live dependencies, so this is importable from the scanner,
the strategies, and (eventually) the backtester alike. Operate on a DataFrame
with lowercase ``open/high/low/close/volume`` columns; ``bars_to_df`` builds one
from a list of ``OHLCVBar``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.data_sources.base import OHLCVBar


def bars_to_df(bars: Sequence[OHLCVBar]) -> pd.DataFrame:
    """OHLCVBar list → float DataFrame (open/high/low/close/volume)."""
    return pd.DataFrame(
        {
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        }
    )


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def cci(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Commodity Channel Index: (tp - SMA(tp)) / (0.015 * mean-abs-dev)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low = df["high"], df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    """Supertrend trail (line-for-line from ``backtest_engine.calc_supertrend``)."""
    a = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    ub = (hl2 + multiplier * a).values.copy()
    lb = (hl2 - multiplier * a).values.copy()
    close = df["close"].values
    n = len(df)

    st = np.full(n, np.nan)
    d = np.ones(n, dtype=int)
    for i in range(period, n):
        if close[i] > ub[i - 1]:
            d[i] = 1
        elif close[i] < lb[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
        if d[i] == 1:
            if d[i - 1] == 1:
                lb[i] = max(lb[i], lb[i - 1])
            st[i] = lb[i]
        else:
            if d[i - 1] == -1:
                ub[i] = min(ub[i], ub[i - 1])
            st[i] = ub[i]
    return pd.Series(st, index=df.index)
