"""
9/15 EMA crossover — swing-crypto strategy.

Trades a textbook short-term trend signal: when the 9-period EMA crosses
above the 15-period EMA on the previous-to-latest closed bar, treat that
as a long entry signal. Cross-down is reserved for exits (handled by the
runner's exit hook in a later iteration; v1 leaves exits to the dedup
gate and reconciler).

Inputs come from whatever ``MarketData`` adapter ``BucketRunner`` injects.
Per Decision 004, crypto signals are sourced from Binance; that wiring
lives in ``run_bot.py``. For Phase-1 simplicity the bot currently passes
the Delta India adapter — both expose the same ``get_ohlcv`` interface,
so the strategy doesn't care.

NB: EMA values use ``pandas.Series.ewm(adjust=False)`` so the recursion
matches the canonical TradingView/MT4 definition (no warm-up debiasing).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pandas as pd

from src.core.logging import get_logger
from src.data_sources.base import MarketData
from src.shared.base_strategy import EntryCandidate, Strategy

if TYPE_CHECKING:
    from src.core.models import MarketRegime, Position

_log = get_logger("strategies.swing.crypto.ema_9_15")

_FAST: int = 9
_SLOW: int = 15
# Need at least slow+2 closes so we can read positions [-2] and [-1].
_MIN_BARS: int = _SLOW + 2


class Ema9_15Crossover(Strategy):
    """Long when EMA(9) just crossed above EMA(15) on the most recent bar."""

    name: str = "ema_9_15"
    tf: str = "1h"
    trading_type: str = "swing"

    def select_entries(
        self,
        candidates: list[str],
        data: MarketData,
    ) -> list[EntryCandidate]:
        out: list[EntryCandidate] = []
        for sym in candidates:
            try:
                bars = data.get_ohlcv(sym, self.tf, limit=_MIN_BARS + 30)
            except Exception:
                _log.warning("ohlcv_fetch_failed", symbol=sym, exc_info=True)
                continue

            if len(bars) < _MIN_BARS:
                _log.debug(
                    "insufficient_bars", symbol=sym, got=len(bars), need=_MIN_BARS
                )
                continue

            closes = pd.Series([float(b.close) for b in bars])
            ema_fast = closes.ewm(span=_FAST, adjust=False).mean()
            ema_slow = closes.ewm(span=_SLOW, adjust=False).mean()

            # Cross-up detection: at bar t-1 the fast was ≤ slow,
            # at bar t (the most recently closed bar) the fast is > slow.
            prev_fast = ema_fast.iloc[-2]
            prev_slow = ema_slow.iloc[-2]
            curr_fast = ema_fast.iloc[-1]
            curr_slow = ema_slow.iloc[-1]

            crossed_up = prev_fast <= prev_slow and curr_fast > curr_slow
            if not crossed_up:
                continue

            out.append(
                EntryCandidate(
                    symbol=sym,
                    side="buy",
                    hint={
                        "signal": "ema_9_15_cross_up",
                        "ema_fast": round(curr_fast, 6),
                        "ema_slow": round(curr_slow, 6),
                        "gap_bps": round(
                            10000 * (curr_fast - curr_slow) / curr_slow, 2
                        ),
                    },
                )
            )
            _log.info(
                "ema_cross_up",
                symbol=sym,
                ema_fast=round(curr_fast, 6),
                ema_slow=round(curr_slow, 6),
            )

        return out

    def select_exits(
        self,
        held: dict[str, Position],
        data: MarketData,
        regimes: Mapping[str, MarketRegime | None] | None = None,  # noqa: ARG002
    ) -> list[str]:
        """Exit a held long once EMA(9) sits below EMA(15).

        State-based rather than cross-based so a missed cross bar (bot
        restart, tick gap) still exits on the next tick.
        """
        exits: list[str] = []
        for sym in held:
            try:
                bars = data.get_ohlcv(sym, self.tf, limit=_MIN_BARS + 30)
            except Exception:
                _log.warning("exit_ohlcv_fetch_failed", symbol=sym, exc_info=True)
                continue
            if len(bars) < _MIN_BARS:
                continue

            closes = pd.Series([float(b.close) for b in bars])
            ema_fast = closes.ewm(span=_FAST, adjust=False).mean()
            ema_slow = closes.ewm(span=_SLOW, adjust=False).mean()

            if ema_fast.iloc[-1] < ema_slow.iloc[-1]:
                exits.append(sym)
                _log.info(
                    "ema_exit_signal",
                    symbol=sym,
                    ema_fast=round(ema_fast.iloc[-1], 6),
                    ema_slow=round(ema_slow.iloc[-1], 6),
                )
        return exits
