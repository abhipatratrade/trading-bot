"""
NIFTY-100 gap-down reversal — long-only intraday fade (Decision 029).

backtest_ref: ``Backtesting Engine/strategies/optimized/nifty100_gap_reversal/``
  (TRADING_BOT_HANDOFF.md + nifty100_gap_reversal_rules.json; findings in
  results/learnings/gap_reversal_nifty100_2026-07-19.md)

  Frozen config, holdout-validated 2026-07-19 on 24 months of NIFTY-100 Dhan 5m:
      TRAIN   2025-07→2026-07   41 trades  +23.3%  wr 61%  PF 2.34  DD 5.8%
      HOLDOUT 2024-07→2025-06   35 trades  +13.3%  wr 51%  PF 1.68  DD 9.2%
  The holdout fold is frozen and contains the Apr-2025 crash. Plan around THAT
  grade (~PF 1.7, ~+13%/yr on deployed margin), not the full-period +36.6%.

The scanner (``engine: equity_intraday``) has already cut the morning universe to
names that gapped DOWN 3–12% with a first-15m body ≥ 25% of daily ATR(14). This
strategy adds the trigger and the exit:

  ENTRY  the first 5m candle closing in [09:30, 10:30] that is a bullish
         **engulfing** or **hammer** → go LONG. One trade per symbol per day.
  EXIT   hold to the 15:15 IST square-off. No stop, no target.

Why no stop: every tight-stop variant tested LOSES (pattern-low stop RR 1–3,
5m trailing) — the reversal needs hours to play out and noise-stops fire first.
The bucket still carries a WIDE broker-side catastrophe stop from buckets.yaml
(Decision 029 §stops), far outside normal behaviour, purely for a crash while
the bot is down.

Do NOT: short, fade gap-ups, lower the 3% gap floor, enter after 10:30, or add a
tight stop. All four were tested and rejected.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import TYPE_CHECKING, ClassVar

from src.core.logging import get_logger
from src.shared.base_strategy import EntryCandidate, Strategy
from src.shared.scanner import indicators as ind
from src.shared.scanner.gap_reversal import ist_date, ist_time, session_bars
from src.shared.scanner.patterns import BODY_AVG_LEN, pattern_flags

if TYPE_CHECKING:
    from src.core.models import MarketRegime, Position
    from src.data_sources.base import MarketData, OHLCVBar

_log = get_logger("strategies.intraday.indian.gap_down_reversal")

# Entry window (IST). Bar timestamps are bar-OPEN times throughout this repo
# (see equity.py's 09:45 handling), so a "candle closing at 09:30" is the bar
# STAMPED 09:25 — the third of the session. The frozen config cuts off when the
# ENTRY bar (the one after the pattern) would open past 10:30, so the last
# actionable pattern bar is stamped 10:25.
_PATTERN_FIRST_BAR = time(9, 25)   # closes 09:30
_PATTERN_LAST_BAR = time(10, 25)   # entry bar opens 10:30
# Square-off (IST). MIS product means the broker also force-closes ~15:20 as a
# backstop if our own exit somehow doesn't land.
_SQUAREOFF = time(15, 15)

# The body-average EMA needs history to settle; below this many session bars the
# pattern flags aren't trustworthy, so we don't trade the name that day.
_MIN_BARS = BODY_AVG_LEN + 3

# How stale a signal may be and still be entered, in 5m bars. The backtest fills
# at the next bar's open, so ideally the pattern bar is the last CLOSED bar.
# Allowing 1 extra bar absorbs the one thing we can't assume about the feed —
# whether Dhan's 5m response includes the current in-progress candle — without
# widening far enough to let a genuinely stale signal (bot restart) through.
_MAX_SIGNAL_AGE_BARS = 1


class GapDownReversal(Strategy):
    """Fade a 3–12% NIFTY-100 gap-down on the first bullish 5m reversal candle."""

    name: ClassVar[str] = "gap_down_reversal"
    tf: ClassVar[str] = "5m"
    trading_type: ClassVar[str] = "intraday"

    def select_entries(
        self,
        candidates: list[str],
        data: MarketData,
    ) -> list[EntryCandidate]:
        """Long the scanner's gapped-down names on their first reversal candle.

        LIVE-vs-BACKTEST (deliberate): the backtest enters at the OPEN of the bar
        AFTER the pattern candle. Live, the equivalent is "the pattern candle is
        the bar that just closed" — we then send a market order, which fills at
        essentially that next bar's open. So a pattern is only actionable while
        it is the MOST RECENTLY CLOSED bar; a hit discovered late (bot restart at
        10:00 for a 09:35 signal) is deliberately skipped rather than entered at
        a price the backtest never saw.
        """
        out: list[EntryCandidate] = []
        for symbol in candidates:
            try:
                bars = data.get_ohlcv(symbol, self.tf)
            except Exception:
                _log.warning("entry_ohlcv_fetch_failed", symbol=symbol, exc_info=True)
                continue

            hit = self._first_reversal(bars)
            if hit is None:
                continue
            index, pattern = hit

            today = self._today_bars(bars)
            # Actionable only while the pattern candle is (about) the last
            # closed bar — see _MAX_SIGNAL_AGE_BARS.
            age = len(today) - 1 - index
            if age > _MAX_SIGNAL_AGE_BARS:
                _log.debug(
                    "gap_reversal_signal_stale",
                    symbol=symbol,
                    pattern=pattern,
                    bars_since=age,
                )
                continue

            out.append(
                EntryCandidate(
                    symbol=symbol,
                    side="buy",
                    hint={
                        "pattern": pattern,
                        "pattern_close_ist": ist_time(today[index]).isoformat(),
                    },
                )
            )
            _log.info("gap_reversal_entry_signal", symbol=symbol, pattern=pattern)
        return out

    def select_exits(
        self,
        held: dict[str, Position],
        data: MarketData,
        regimes: Mapping[str, MarketRegime | None] | None = None,  # noqa: ARG002
    ) -> list[str]:
        """Square off every held position at/after 15:15 IST. No other exit.

        Time is read off the latest BAR rather than a wall clock so the same
        code replays identically in the backtester (House Rule 9). If the data
        feed stalls, the broker's MIS auto-square-off (~15:20) is the backstop.
        """
        exits: list[str] = []
        for symbol in held:
            try:
                bars = data.get_ohlcv(symbol, self.tf)
            except Exception:
                _log.warning("exit_ohlcv_fetch_failed", symbol=symbol, exc_info=True)
                continue
            if not bars:
                continue
            latest = max(bars, key=lambda b: b.timestamp)
            if ist_time(latest) >= _SQUAREOFF:
                exits.append(symbol)
                _log.info(
                    "gap_reversal_squareoff",
                    symbol=symbol,
                    bar_ist=ist_time(latest).isoformat(),
                )
        return exits

    # ---- internals -------------------------------------------------------
    def _today_bars(self, bars: list[OHLCVBar]) -> list[OHLCVBar]:
        """Today's session bars, chronological. 'Today' = the latest bar's date."""
        if not bars:
            return []
        latest = max(bars, key=lambda b: b.timestamp)
        return session_bars(bars, ist_date(latest))

    def _first_reversal(self, bars: list[OHLCVBar]) -> tuple[int, str] | None:
        """Index (into today's bars) + name of the FIRST qualifying candle.

        Returns the first bullish engulfing/hammer stamped inside
        [09:25, 10:25] — i.e. closing between 09:30 and the last entry bar.
        "First of the day" is what enforces one trade per symbol per day: a
        second pattern at 10:15 is never returned once one printed at 09:35.

        CRITICAL — the pattern flags are computed over the FULL multi-session
        bar series, never over today alone. ``body_avg`` is a 14-EMA of candle
        body size, so restarting it at 09:15 would seed it from the opening
        bars, which on a gap-down morning are the largest of the day. That
        inflates the average, ``long_body`` stops firing, and bullish
        engulfing all but disappears — measured against the 76 frozen backtest
        trades, session-scoped flags reproduced only 33 of them vs 76 for
        full-series flags. Keep the whole series.
        """
        ordered = sorted(bars, key=lambda b: b.timestamp)
        if len(ordered) < _MIN_BARS:
            return None
        day = ist_date(ordered[-1])
        today_idx = [i for i, b in enumerate(ordered) if ist_date(b) == day]
        if len(today_idx) < 3:
            return None

        flags = pattern_flags(ind.bars_to_df(ordered))
        for local, full in enumerate(today_idx):
            bar_open = ist_time(ordered[full])
            if bar_open < _PATTERN_FIRST_BAR:
                continue
            if bar_open > _PATTERN_LAST_BAR:
                return None
            if bool(flags["engulfing_bull"].iloc[full]):
                return local, "engulfing_bull"
            if bool(flags["hammer"].iloc[full]):
                return local, "hammer"
        return None
