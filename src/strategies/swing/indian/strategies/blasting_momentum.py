"""
Blasting Momentum — swing-indian strategy (Phase 4, Dhan).

backtest_ref:
    Backtesting Engine/results/learnings/2026-07-09_blasting_momentum_swing.md
    Nifty 500, ~1yr: +23.5% unlevered (PF 1.71, win 54%, DD 7.3%, 41 trades,
    OOS halves +9.7%/+10.5%). Live mode: 3x MTF (bucket leverage_max=4).

Division of labour (mirrors the backtest exactly):
  * scanner.yaml does ALL entry filtering at 09:45 IST (gap>=2% vs prev close,
    daily RSI(14)>=65 & rising, EMA10>EMA20, CCI(14)>=200, volume/price/turnover
    floors) and ranks by gap %. By the time ``select_entries`` runs, candidates
    are already the ranked survivors — the strategy just claims free slots.
  * ``select_exits`` owns the exit: daily Supertrend(10,3) flip below the close,
    or a 30-calendar-day hold cap. NO intraday stop — the backtest showed tight
    stops destroy this edge (day-1 noise knocks out eventual winners).

Supertrend (and the other indicators) live in ``src.shared.scanner.indicators``
— a vendored byte-for-byte copy of the backtest engine's ``calc_*`` — so the
scanner and this strategy share one implementation and live exits match
backtested exits bit-for-bit.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from src.core.logging import get_logger
from src.data_sources.base import MarketData
from src.shared.base_strategy import EntryCandidate, Strategy
from src.shared.scanner.indicators import bars_to_df, supertrend

if TYPE_CHECKING:
    from src.core.models import MarketRegime, Position

_log = get_logger("strategies.swing.indian.blasting_momentum")

_ST_PERIOD: int = 10
_ST_MULT: float = 3.0
_MAX_HOLD_DAYS: int = 30
_MIN_BARS: int = _ST_PERIOD + 5


class BlastingMomentum(Strategy):
    """Buy the scanner's ranked gap-momentum basket; exit on daily ST flip / 30d."""

    name: str = "blasting_momentum"
    tf: str = "1d"
    trading_type: str = "swing"

    def select_entries(
        self,
        candidates: list[str],
        data: MarketData,
    ) -> list[EntryCandidate]:
        # scanner.yaml has already filtered AND ranked (gap_up_pct_desc);
        # order is preserved so the runner fills slots from the strongest gap.
        out: list[EntryCandidate] = []
        for sym in candidates:
            out.append(EntryCandidate(symbol=sym, side="buy",
                                      hint={"signal": "blasting_momentum_scan"}))
            _log.info("entry_candidate", symbol=sym)
        return out

    def select_exits(
        self,
        held: dict[str, Position],
        data: MarketData,
        regimes: Mapping[str, MarketRegime | None] | None = None,  # noqa: ARG002
    ) -> list[str]:
        """Exit when today's daily close < Supertrend(10,3), or held >= 30 days.

        State-based (close below the trail), not cross-based, so a missed tick
        or bot restart still exits on the next evaluation.
        """
        exits: list[str] = []
        now = datetime.now(UTC)
        for sym, pos in held.items():
            # --- 30-day hold cap (backtest-optimal: 30d beat 20d and infinity)
            opened = getattr(pos, "opened_at", None) or getattr(pos, "created_at", None)
            if opened is not None:
                opened_utc = opened if opened.tzinfo else opened.replace(tzinfo=UTC)
                if (now - opened_utc).days >= _MAX_HOLD_DAYS:
                    exits.append(sym)
                    _log.info("exit_max_days", symbol=sym, days=(now - opened_utc).days)
                    continue

            # --- daily Supertrend flip
            try:
                bars = data.get_ohlcv(sym, self.tf, limit=_MIN_BARS + 60)
            except Exception:
                _log.warning("exit_ohlcv_fetch_failed", symbol=sym, exc_info=True)
                continue
            if len(bars) < _MIN_BARS:
                continue
            df = bars_to_df(bars)
            st = supertrend(df, _ST_PERIOD, _ST_MULT)
            close = float(df["close"].iloc[-1])
            trail = float(st.iloc[-1])
            if np.isnan(trail):
                continue
            if close < trail:
                exits.append(sym)
                _log.info("exit_st_flip", symbol=sym,
                          close=round(close, 2), supertrend=round(trail, 2))
        return exits
