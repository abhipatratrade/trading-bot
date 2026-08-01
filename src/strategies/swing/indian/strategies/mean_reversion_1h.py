"""
Midcap-150 1h mean reversion — swing-indian, long-only (Decision 032).

backtest_ref: ``Backtesting Engine/strategies/optimized/midcap150_meanrev_1h_swing/``
  (TRADING_BOT_HANDOFF.md + mean_reversion_1h_swing_rules.json; findings in
  results/learnings/2026-07-23_meanrev_1h_nifty100.md)

  Frozen config, holdout-validated 2026-07-24 on 24 months of Dhan 15m→1h + 1D
  over NIFTY-Midcap-150 ∩ NSE F&O (94 names), ₹10k margin/trade at per-stock MTF:
      TRAIN   2025-07→2026-07    82 trades  +24.7%  wr ~72%  PF 2.31  DD 4.4%
      HOLDOUT 2024-06→2025-06   132 trades  +57.1%  wr ~75%  PF 3.04  DD 5.3%
  Plan around the TRAIN grade (~PF 2.3, ~+25%/yr on deployed margin, ~4–5% DD),
  less ~4% for MTF interest. The holdout is crash-boosted — this is a convex
  buy-the-panic book: flat-to-small in calm tape, strong through corrections.

Division of labour:
  * ``scanner.yaml`` (engine ``equity_meanrev_1h``) evaluates all 94 names on
    each completed 1h bin, keeps FRESH downward crosses of −6.5% from the 1h
    EMA20, ranks by depth and applies the ≤5-new-entries-per-day budget.
  * ``select_entries`` re-derives the signal for those few names (so the entry
    is a pure function of bars, not of a DB row), enforces the "act on it while
    it is still the freshly-closed bar" rule, and hands the sizer the protective
    stop distance (3.5 × daily ATR14) for the resting broker-side stop.
  * ``select_exits`` owns the primary exit: a completed 1h bar closing at or
    above the EMA20 (mean touch), plus a 20-trading-day backstop.

Do NOT (each tested and rejected in-sample — see the learnings file):
  * add shorts or trade both sides — net-negative in this universe;
  * add a market-regime gate — "index above SMA" cut net +30.7%→+2–9% by
    removing the crash-rebound trades that ARE the edge;
  * lower the 6.5% threshold — 4.5% is net-negative falling-knife territory;
  * ATR-normalise position size, or exclude the high-beta names (IDEA, RVNL,
    MCX… drive 53% of net; per-stock MTF leverage already de-risks them).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from src.core.logging import get_logger
from src.shared.base_strategy import EntryCandidate, Strategy
from src.shared.market_calendar import is_trading_day
from src.shared.scanner.meanrev import (
    IST,
    MeanRevConfig,
    evaluate,
    last_complete_bar_key,
    mean_touched,
)

if TYPE_CHECKING:
    from src.core.models import MarketRegime, Position
    from src.data_sources.base import MarketData

_log = get_logger("strategies.swing.indian.mean_reversion_1h")

# The frozen config, duplicated from scanner.yaml so the strategy is a complete
# description of the trade on its own. ``scanner.yaml`` is the authority the
# scan reads; these must agree, and the parity test asserts it.
_EMA_LEN = 20
_DIST_THRESHOLD = "6.5"
_ATR_PERIOD = 14
_STOP_ATR_MULT = "3.5"
_MAX_HOLD_TRADING_DAYS = 20
_INTRADAY_LOOKBACK_DAYS = 90


class MeanReversion1h(Strategy):
    """Buy a fresh −6.5% dislocation below the 1h EMA20; exit on the mean touch."""

    name: ClassVar[str] = "mean_reversion_1h"
    tf: ClassVar[str] = "1h"
    trading_type: ClassVar[str] = "swing"

    def _cfg(self) -> MeanRevConfig:
        from decimal import Decimal

        return MeanRevConfig(
            universe_size=5,
            symbols=(),
            ema_len=_EMA_LEN,
            dist_threshold=Decimal(_DIST_THRESHOLD),
            atr_period=_ATR_PERIOD,
            stop_atr_mult=Decimal(_STOP_ATR_MULT),
            max_hold_days=_MAX_HOLD_TRADING_DAYS,
            daily_entry_cap=5,
            intraday_lookback_days=_INTRADAY_LOOKBACK_DAYS,
            scale_tolerance=Decimal("0.03"),
            fno_only=True,
        )

    # ---- entries ---------------------------------------------------------
    def select_entries(
        self,
        candidates: list[str],
        data: MarketData,
    ) -> list[EntryCandidate]:
        """Long the scanner's freshly-dislocated names, at the next bar's open.

        LIVE-vs-BACKTEST (deliberate, same convention as gap_down_reversal): the
        backtest fills at the OPEN of the 1h bar AFTER the bar whose close
        triggered the cross. Live, we detect the cross moments after that bar
        closes and send a market order, which fills at essentially that next
        bar's open. ``want_bar_key`` therefore pins the signal to the bar that
        just closed — a hit discovered late (bot restart mid-bin) is skipped
        rather than entered at a price the backtest never saw.

        The 15:15→15:30 stub bin is a signal bar like any other; its "next
        bar's open" is the following session's 09:15, which is exactly when
        ``last_complete_bar_key`` still names it. Three of the 214 backtest
        trades entered that way.
        """
        cfg = self._cfg()
        want = last_complete_bar_key(datetime.now(UTC))
        out: list[EntryCandidate] = []
        for symbol in candidates:
            try:
                intraday = self._intraday(data, symbol, cfg.intraday_lookback_days)
                daily = data.get_ohlcv(symbol, "1d", limit=cfg.atr_period + 40)
            except Exception:
                _log.warning("entry_ohlcv_fetch_failed", symbol=symbol, exc_info=True)
                continue

            sig = evaluate(symbol, intraday, daily, cfg, want_bar_key=want)
            if sig is None:
                _log.debug("meanrev_entry_not_confirmed", symbol=symbol, bar=want)
                continue

            out.append(
                EntryCandidate(
                    symbol=symbol,
                    side="buy",
                    hint={
                        "signal": "meanrev_1h_fresh_cross",
                        "bar_key": sig.bar_key,
                        # Decision 033: the close of the bar the decision was
                        # made on. The backtest fills at the NEXT bar's open,
                        # so this is the reference the live fill is measured
                        # against — the gap between them IS the live-vs-backtest
                        # execution difference, and it is unrecoverable unless
                        # recorded here.
                        "signal_price": str(sig.close),
                        "dist_pct": str(sig.dist_pct),
                        "ema20": str(sig.ema20),
                        "atr14": str(sig.atr14),
                        # Decision 032: the protective stop is a RESTING
                        # broker-side order at entry − 3.5 × daily ATR14, so it
                        # survives the bot/VM being down. The runner forwards
                        # this distance to the stop sweep via the Trade row.
                        "stop_distance": str(sig.stop_distance),
                    },
                )
            )
            _log.info(
                "meanrev_entry_signal",
                symbol=symbol,
                bar=sig.bar_key,
                dist_pct=str(sig.dist_pct),
                stop_distance=str(sig.stop_distance),
            )
        return out

    # ---- exits -----------------------------------------------------------
    def select_exits(
        self,
        held: dict[str, Position],
        data: MarketData,
        regimes: Mapping[str, MarketRegime | None] | None = None,  # noqa: ARG002
    ) -> list[str]:
        """Exit on the mean touch (1h close ≥ EMA20), or after 20 trading days.

        Both are state-based, not cross-based, so a missed tick or a restart
        still exits on the next evaluation. The third exit — the 3.5 × ATR
        protective stop — is NOT here: it rests on the exchange (Decision 022 /
        032), which is both faithful to the backtest's gap-through modelling and
        the only version that protects while the bot is down.

        The mean touch is pinned to the last COMPLETE bin, exactly as entries
        are. Without that pin it read the bin still forming at HH:16 and exited
        on a poke above the mean that the bar never closed above.
        """
        cfg = self._cfg()
        exits: list[str] = []
        now = datetime.now(UTC)
        want = last_complete_bar_key(now)
        for symbol, pos in held.items():
            opened = getattr(pos, "opened_at", None) or getattr(pos, "created_at", None)
            if opened is not None:
                opened_utc = opened if opened.tzinfo else opened.replace(tzinfo=UTC)
                held_days = _trading_days_between(opened_utc, now)
                if held_days >= cfg.max_hold_days:
                    exits.append(symbol)
                    _log.info(
                        "meanrev_exit_max_hold", symbol=symbol, trading_days=held_days
                    )
                    continue

            try:
                intraday = self._intraday(data, symbol, cfg.intraday_lookback_days)
            except Exception:
                _log.warning("exit_ohlcv_fetch_failed", symbol=symbol, exc_info=True)
                continue
            touch = mean_touched(intraday, cfg.ema_len, want_bar_key=want)
            if touch is None:
                continue
            touched, close, ema = touch
            if touched:
                exits.append(symbol)
                _log.info(
                    "meanrev_exit_mean_touch",
                    symbol=symbol,
                    bar=want,
                    close=str(close),
                    ema20=str(ema),
                )
        return exits

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _intraday(data: MarketData, symbol: str, days: int) -> list:
        if hasattr(data, "get_ohlcv_history"):
            return data.get_ohlcv_history(symbol, "15m", days=days)
        return data.get_ohlcv(symbol, "15m")


def _trading_days_between(start: datetime, end: datetime) -> int:
    """NSE trading days elapsed from ``start`` to ``end`` (IST calendar dates).

    The backtest's cap is 20 TRADING days, not calendar days — over a month with
    a long weekend the two differ by enough to close a position a week early.
    Uses the same holiday set as the session gate, which is deliberately
    conservative (an unlisted holiday just counts one extra day).
    """
    a = start.astimezone(IST).date()
    b = end.astimezone(IST).date()
    if b <= a:
        return 0
    days = 0
    cursor = a + timedelta(days=1)
    while cursor <= b:
        if is_trading_day(cursor):
            days += 1
        cursor += timedelta(days=1)
    return days
