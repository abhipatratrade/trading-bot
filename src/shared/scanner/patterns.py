"""
Candlestick patterns — ported byte-for-byte from the Backtesting Engine
(``candlestick_patterns._detect_all``), which is itself a 1:1 port of
TradingView's built-in "All Candlestick Patterns" indicator.

Only the two patterns the gap-reversal strategy trades are ported:
``engulfing_bull`` and ``hammer``. Both are evaluated with TradingView's
"Detect Trend Based On" set to **No detection** (``trend_rule="none"`` in the
engine), which makes the trend terms identically True — so they are dropped
here rather than carried as a constant. Adding a pattern later means porting
its line from the engine module, not inventing one.

Same rationale as ``indicators.py``: the backtester is a separate repo
(Decision "Backtest engine out of scope"), so this is a vendored copy rather
than an import. The frozen validated config that depends on this math is
``strategies/optimized/nifty100_gap_reversal/`` in the engine repo.

Verify against a chart with the engine's
``strategies/optimized/nifty100_gap_reversal/nifty100_gap_reversal.pine``.
"""

from __future__ import annotations

import pandas as pd

# TradingView "All Candlestick Patterns" inputs (engine constants of the same
# name). Changing these silently de-syncs live from the backtest.
BODY_AVG_LEN = 14      # C_Len — EMA depth for the average body size
SHADOW_PCT = 5.0       # C_ShadowPercent — "has a shadow" threshold, % of body
FACTOR = 2.0           # C_Factor — shadow dominance multiple (hammer)


def _bshift(s: pd.Series, n: int = 1) -> pd.Series:
    """Shift a boolean Series without upcasting to object (missing → False)."""
    return s.shift(n, fill_value=False)


def pattern_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar ``engulfing_bull`` / ``hammer`` booleans for an OHLC frame.

    ``df`` needs lowercase ``open/high/low/close`` columns (use
    ``indicators.bars_to_df``). Returns a frame indexed like ``df`` with one
    boolean column per pattern; row *i* answers "is the candle that CLOSED at
    bar i this pattern?".

    CALLER CONTRACT — pass the FULL continuous bar series, not a slice.
    ``body_avg`` is a 14-EMA of body size that carries across sessions in the
    engine. Restarting it at a session boundary (let alone at the entry window)
    seeds it from the opening candles, which on a gap morning are the day's
    largest; ``long_body`` then stops firing and ``engulfing_bull`` nearly
    vanishes. Measured against the 76 frozen gap-reversal backtest trades,
    session-scoped input reproduced 33; full-series input reproduces all 76.
    """
    o, h, low_, c = df["open"], df["high"], df["low"], df["close"]

    body_hi = pd.concat([o, c], axis=1).max(axis=1)   # C_BodyHi
    body_lo = pd.concat([o, c], axis=1).min(axis=1)   # C_BodyLo
    body = body_hi - body_lo                          # C_Body
    body_avg = body.ewm(span=BODY_AVG_LEN, adjust=False).mean()  # C_BodyAvg
    up_sh = h - body_hi                               # C_UpShadow
    dn_sh = body_lo - low_                            # C_DnShadow
    hl2 = (h + low_) / 2

    small = body < body_avg                           # C_SmallBody
    long_ = body > body_avg                           # C_LongBody
    has_up = up_sh > SHADOW_PCT / 100 * body          # C_HasUpShadow
    white = o < c                                     # C_WhiteBody
    black = o > c                                     # C_BlackBody

    # Hammer: small body sitting in the upper half of the range, with a lower
    # shadow at least FACTOR× the body and effectively no upper shadow.
    # (trend_rule "none" ⇒ the engine's `& down_trend` term is always True.)
    hammer = (
        small
        & (body > 0)
        & (body_lo > hl2)
        & (dn_sh >= FACTOR * body)
        & ~has_up
    )

    # Bullish engulfing: long white body swallowing the prior small black body.
    engulfing_bull = (
        white
        & long_
        & _bshift(black)
        & _bshift(small)
        & (c >= o.shift(1))
        & (o <= c.shift(1))
        & ((c > o.shift(1)) | (o < c.shift(1)))
    )

    return pd.DataFrame(
        {"engulfing_bull": engulfing_bull, "hammer": hammer}, index=df.index
    )
