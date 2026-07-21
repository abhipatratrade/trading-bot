"""Gap-down reversal (intraday-indian) — patterns, morning screen, strategy.

backtest_ref: Backtesting Engine/strategies/optimized/nifty100_gap_reversal/
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from src.data_sources.base import OHLCVBar
from src.shared.bucket import TradingType, load_bucket
from src.shared.market_calendar import NseSession, nse_session, parse_ist_time
from src.shared.scanner import gap_reversal as gr
from src.shared.scanner import indicators as ind
from src.shared.scanner.engine import load_scanner_config
from src.shared.scanner.patterns import pattern_flags
from src.strategies.intraday.indian.strategies.gap_down_reversal import (
    GapDownReversal,
)

_SCANNER_YAML = Path("src/strategies/intraday/indian/scanner.yaml")
_IST = gr.IST
_DAY = date(2026, 7, 20)  # a Monday


def _cfg() -> gr.GapReversalConfig:
    return gr.GapReversalConfig.from_scanner_config(
        load_scanner_config(_SCANNER_YAML)
    )


def _bar(
    day: date, hhmm: tuple[int, int], o: float, h: float, low: float, c: float
) -> OHLCVBar:
    ts = datetime(
        day.year, day.month, day.day, hhmm[0], hhmm[1], tzinfo=_IST
    ).astimezone(UTC)
    return OHLCVBar(
        timestamp=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal("50000"),
    )


def _flat_session(day: date, price: float, n: int = 40) -> list[OHLCVBar]:
    """A calm session of small candles starting 09:15, 5m apart."""
    out = []
    t = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
    for _ in range(n):
        out.append(
            _bar(
                day,
                (t.hour, t.minute),
                price,
                price + 0.5,
                price - 0.5,
                price + 0.1,
            )
        )
        t += timedelta(minutes=5)
    return out


def _daily(n: int, close: float) -> list[OHLCVBar]:
    """Flat daily bars with a ~2% true range → ATR ≈ 2% of price."""
    out = []
    for i in range(n):
        out.append(
            OHLCVBar(
                timestamp=datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=i),
                open=Decimal(str(close)),
                high=Decimal(str(close * 1.01)),
                low=Decimal(str(close * 0.99)),
                close=Decimal(str(close)),
                volume=Decimal("1000000"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Pattern math
# ---------------------------------------------------------------------------
def test_engulfing_bull_detected() -> None:
    """Small black candle, then a long white one that swallows its body."""
    day = _DAY
    bars = _flat_session(day, 100.0, n=20)
    # body_avg settles near 0.1 on the flat session, so the prior candle must
    # have a body BELOW that to count as small, and the engulfer above it.
    bars.append(_bar(day, (10, 55), 100.05, 100.06, 99.99, 100.00))  # small black
    bars.append(_bar(day, (11, 0), 99.95, 101.10, 99.90, 101.00))    # long white
    flags = pattern_flags(ind.bars_to_df(bars))
    assert bool(flags["engulfing_bull"].iloc[-1])
    assert not bool(flags["hammer"].iloc[-1])


def test_hammer_detected() -> None:
    """Small body at the top of the range with a long lower shadow."""
    day = _DAY
    bars = _flat_session(day, 100.0, n=20)
    # body 100.00->100.05 (small), lower shadow 0.10 (>= 2x body), no upper shadow.
    bars.append(_bar(day, (11, 0), 100.00, 100.05, 99.90, 100.05))
    flags = pattern_flags(ind.bars_to_df(bars))
    assert bool(flags["hammer"].iloc[-1])


def test_body_average_is_ema_over_full_series_not_a_slice() -> None:
    """Regression: slicing before ``pattern_flags`` changes the signal.

    ``body_avg`` is a 14-EMA of body size. Restarting it at a session boundary
    seeds it from the (large) opening candles, which suppresses ``long_body``
    and makes bullish engulfing all but disappear. Measured against the 76
    frozen backtest trades this was the difference between 33 and 76
    reproduced — so it is pinned here.
    """
    day = _DAY
    prior = _flat_session(date(2026, 7, 17), 100.0, n=40)
    # Wide opening candles (body 3.0 vs the prior session's 0.1), then the same
    # engulfing pair as above. Restarting the EMA here lifts body_avg to ~3;
    # carrying it from the prior session leaves it under 1, which is the whole
    # difference between seeing the engulfer and missing it.
    today = [
        _bar(day, (9, 15), 100.0, 105.0, 95.0, 97.0),
        _bar(day, (9, 20), 97.0, 101.0, 94.0, 100.0),
        _bar(day, (9, 25), 100.0, 104.0, 96.0, 97.0),
        _bar(day, (9, 30), 100.05, 100.06, 99.99, 100.00),
        _bar(day, (9, 35), 99.95, 101.10, 99.90, 101.00),
    ]
    full = pattern_flags(ind.bars_to_df(prior + today))
    sliced = pattern_flags(ind.bars_to_df(today))
    assert bool(full["engulfing_bull"].iloc[-1]), "full series must see it"
    assert not bool(sliced["engulfing_bull"].iloc[-1]), (
        "session-sliced body_avg is inflated by the opening candles and misses it"
    )


# ---------------------------------------------------------------------------
# Morning screen
# ---------------------------------------------------------------------------
def _screen_inputs(
    gap_pct: float, *, body_frac: float = 0.02, prev: float = 100.0
) -> tuple[list[OHLCVBar], list[OHLCVBar]]:
    """5m bars spanning a prior session + today's open at the requested gap."""
    prior = _flat_session(date(2026, 7, 17), prev, n=40)
    # Force the prior session's LAST close to exactly ``prev``.
    prior[-1] = _bar(date(2026, 7, 17), (12, 30), prev, prev, prev, prev)
    open_0915 = prev * (1 + gap_pct / 100)
    close_0930 = open_0915 * (1 - body_frac)
    today = [
        _bar(_DAY, (9, 15), open_0915, open_0915 * 1.01, close_0930 * 0.99, open_0915),
        _bar(_DAY, (9, 20), open_0915, open_0915, close_0930, close_0930),
        _bar(_DAY, (9, 25), close_0930, close_0930 * 1.005, close_0930 * 0.99, close_0930),
        _bar(_DAY, (9, 30), close_0930, close_0930, close_0930, close_0930),
        _bar(_DAY, (9, 35), close_0930, close_0930, close_0930, close_0930),
        _bar(_DAY, (9, 40), close_0930, close_0930, close_0930, close_0930),
    ]
    return prior + today, _daily(30, prev)


def test_gap_screen_accepts_a_clean_gap_down() -> None:
    intraday, daily = _screen_inputs(-5.0)
    cand = gr.gap_screen("TEST", intraday, daily, _DAY, _cfg())
    assert cand is not None
    assert cand.gap_pct < 0
    assert round(float(cand.gap_pct), 1) == -5.0


def test_gap_screen_rejects_shallow_and_extreme_gaps() -> None:
    cfg = _cfg()
    for gap in (-2.0, -13.0):
        intraday, daily = _screen_inputs(gap)
        assert gr.gap_screen("TEST", intraday, daily, _DAY, cfg) is None, gap


def test_gap_screen_rejects_gap_ups() -> None:
    """Long-only: the short side had no edge and is never scanned."""
    intraday, daily = _screen_inputs(+5.0)
    assert gr.gap_screen("TEST", intraday, daily, _DAY, _cfg()) is None


def test_gap_screen_rejects_weak_first_15m_body() -> None:
    """Body below 25% of daily ATR (~2% here) = indecisive open, no trade."""
    intraday, daily = _screen_inputs(-5.0, body_frac=0.001)
    assert gr.gap_screen("TEST", intraday, daily, _DAY, _cfg()) is None


def test_corporate_action_guard_rejects_rescaled_daily_series() -> None:
    """A split adjusts daily history but never the intraday series."""
    intraday, _ = _screen_inputs(-5.0)
    split_daily = _daily(30, 50.0)  # daily rescaled ×0.5 vs the 5m series
    assert gr.gap_screen("TEST", intraday, split_daily, _DAY, _cfg()) is None


def test_gap_screen_requires_a_clean_0915_open() -> None:
    """No 09:15 bar ⇒ we can't measure the gap; skip rather than guess."""
    intraday, daily = _screen_inputs(-5.0)
    trimmed = [b for b in intraday if gr.ist_time(b) != time(9, 15)]
    assert gr.gap_screen("TEST", trimmed, daily, _DAY, _cfg()) is None


def test_daily_context_ignores_todays_bar() -> None:
    """Immunity to Dhan's late daily candle (2026-07-14 STALE-CLOSE bug)."""
    daily = _daily(30, 100.0)
    poisoned = [
        *daily,
        OHLCVBar(
            timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 12, tzinfo=UTC),
            open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
            close=Decimal("1"), volume=Decimal("1"),
        ),
    ]
    assert gr.daily_context(daily, _DAY, 14) == gr.daily_context(poisoned, _DAY, 14)


def test_rank_top_keeps_largest_gaps() -> None:
    cands = [
        gr.GapCandidate("A", Decimal("100"), Decimal("96"), Decimal("-4"), Decimal("1")),
        gr.GapCandidate("B", Decimal("100"), Decimal("92"), Decimal("-8"), Decimal("1")),
        gr.GapCandidate("C", Decimal("100"), Decimal("95"), Decimal("-5"), Decimal("1")),
    ]
    assert [c.symbol for c in gr.rank_top(cands, 2)] == ["B", "C"]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class _Feed:
    """Minimal MarketData stand-in returning one canned 5m series."""

    def __init__(self, bars: list[OHLCVBar]) -> None:
        self._bars = bars

    def get_ohlcv(self, symbol: str, interval: str, limit: int = 500):  # noqa: ARG002
        return self._bars


def _session_with_signal_at(hhmm: tuple[int, int]) -> list[OHLCVBar]:
    """A gap-down session whose only reversal candle closes at ``hhmm``+5m."""
    prior = _flat_session(date(2026, 7, 17), 100.0, n=40)
    today = []
    t = datetime(_DAY.year, _DAY.month, _DAY.day, 9, 15, tzinfo=_IST)
    while (t.hour, t.minute) < hhmm:
        today.append(_bar(_DAY, (t.hour, t.minute), 95.0, 95.3, 94.7, 95.1))
        t += timedelta(minutes=5)
    # engulfing pair: small black, then long white
    today.append(_bar(_DAY, (t.hour, t.minute), 95.05, 95.06, 94.99, 95.00))
    t += timedelta(minutes=5)
    today.append(_bar(_DAY, (t.hour, t.minute), 94.95, 96.10, 94.90, 96.00))
    return prior + today


def test_strategy_enters_on_reversal_candle() -> None:
    bars = _session_with_signal_at((9, 55))
    out = GapDownReversal().select_entries(["TEST"], _Feed(bars))
    assert [c.symbol for c in out] == ["TEST"]
    assert out[0].side == "buy"
    assert out[0].hint["pattern"] == "engulfing_bull"


def test_strategy_ignores_signal_before_0930_close() -> None:
    """The 09:15 and 09:20 candles are the gap itself, not a reversal."""
    bars = _session_with_signal_at((9, 15))
    assert GapDownReversal().select_entries(["TEST"], _Feed(bars)) == []


def test_strategy_ignores_signal_after_entry_cutoff() -> None:
    """Entry bar would open past 10:30 — the frozen config stops there."""
    bars = _session_with_signal_at((10, 35))
    assert GapDownReversal().select_entries(["TEST"], _Feed(bars)) == []


def test_strategy_skips_stale_signal() -> None:
    """A signal found long after it printed (bot restart) is not chased."""
    bars = _session_with_signal_at((9, 55))
    tail = datetime(_DAY.year, _DAY.month, _DAY.day, 10, 5, tzinfo=_IST)
    for _ in range(4):  # bury the signal well behind the latest bar
        bars.append(_bar(_DAY, (tail.hour, tail.minute), 96.0, 96.1, 95.9, 96.0))
        tail += timedelta(minutes=5)
    assert GapDownReversal().select_entries(["TEST"], _Feed(bars)) == []


def test_strategy_squares_off_at_1515() -> None:
    strat = GapDownReversal()
    held = {"TEST": object()}
    before = _flat_session(_DAY, 100.0, n=1) + [_bar(_DAY, (15, 10), 100, 100, 100, 100)]
    after = before + [_bar(_DAY, (15, 15), 100, 100, 100, 100)]
    assert strat.select_exits(held, _Feed(before)) == []
    assert strat.select_exits(held, _Feed(after)) == ["TEST"]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def test_bucket_config_is_wired() -> None:
    b = load_bucket("intraday-indian")
    assert b.trading_type is TradingType.INTRADAY
    assert b.config.broker.value == "dhan"
    # Rs 1L is load-bearing: 20% cap x 5x = Rs 1L notional, the validated size.
    assert b.config.capital_inr == Decimal("100000")
    assert b.config.leverage_max == Decimal("5")
    assert b.config.entry_start == "09:30"
    assert not b.config.enabled, "ships dark; user flips it on"


def test_scanner_config_uses_intraday_engine_and_full_universe() -> None:
    cfg = load_scanner_config(_SCANNER_YAML)
    assert cfg.engine == "equity_intraday"
    assert cfg.universe_size == 5
    assert len(cfg.symbols) == 99, "NIFTY-100 list the backtest ran on"
    assert len(set(cfg.symbols)) == len(cfg.symbols), "no duplicate constituents"


def test_entry_window_is_per_bucket() -> None:
    """intraday-indian opens at 09:30; swing-indian still opens at 09:45."""
    intraday = load_bucket("intraday-indian").config
    swing = load_bucket("swing-indian").config
    at_0935 = datetime(2026, 7, 20, 9, 35, tzinfo=_IST)
    assert (
        nse_session(
            at_0935,
            parse_ist_time(intraday.entry_start),
            parse_ist_time(intraday.entry_end),
        )
        is NseSession.ENTRY_WINDOW
    )
    assert (
        nse_session(
            at_0935,
            parse_ist_time(swing.entry_start),
            parse_ist_time(swing.entry_end),
        )
        is NseSession.OPEN_NO_ENTRY
    )
