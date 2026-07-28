"""Gap-down reversal (intraday-indian) — patterns, morning screen, strategy.

backtest_ref: Backtesting Engine/strategies/optimized/nifty100_gap_reversal/
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from src.brokers.base import OrderRequest
from src.data_sources.base import OHLCVBar
from src.order_manager.pnl import bucket_cumulative_pnl, bucket_ledger_pnl
from src.shared.allocator.sizer import load_allocator_config
from src.shared.bucket import TradingType, load_bucket
from src.shared.bucket_runner import BucketRunner
from src.shared.market_calendar import NseSession, nse_session, parse_ist_time
from src.shared.scanner import gap_reversal as gr
from src.shared.scanner import indicators as ind
from src.shared.scanner.engine import load_scanner_config
from src.shared.scanner.patterns import pattern_flags
from src.shared.strategy_master.loader import load_strategy_master
from src.strategies.intraday.indian.strategies.gap_down_reversal import (
    GapDownReversal,
)
from src.strategies.intraday.indian.strategies.gap_down_reversal_broad import (
    GapDownReversalBroad,
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
    # 20% cap x 5x on this capital sets the per-trade notional; see
    # allocator.yaml for why Rs 50k stays clear of the cost cliff.
    assert b.config.capital_inr == Decimal("50000")
    assert b.config.leverage_max == Decimal("5")
    assert b.config.entry_start == "09:30"
    # NOTE: config.enabled is operational policy (armed 2026-07-22), not a
    # structural property — deliberately not asserted here.


def test_scanner_config_uses_intraday_engine_and_full_universe() -> None:
    cfg = load_scanner_config(_SCANNER_YAML)
    assert cfg.engine == "equity_intraday"
    assert cfg.universe_size == 5
    assert len(cfg.symbols) == 99, "NIFTY-100 list the backtest ran on"
    assert len(set(cfg.symbols)) == len(cfg.symbols), "no duplicate constituents"


def test_entry_window_is_per_bucket() -> None:
    """Each bucket carries its own window; intraday-indian's still closes 10:30.

    swing-indian's spans the whole session since Decision 032 (1h signal bins
    close at 10:15…15:15, and the stub bin is actioned at the next 09:15 open),
    so the interesting contrast is the END: at 11:00 the intraday bucket is past
    its window while the swing bucket is still inside its own.
    """
    intraday = load_bucket("intraday-indian").config
    swing = load_bucket("swing-indian").config

    def _session(cfg, at):  # noqa: ANN001, ANN202
        return nse_session(
            at, parse_ist_time(cfg.entry_start), parse_ist_time(cfg.entry_end)
        )

    at_0935 = datetime(2026, 7, 20, 9, 35, tzinfo=_IST)
    at_1100 = datetime(2026, 7, 20, 11, 0, tzinfo=_IST)
    assert _session(intraday, at_0935) is NseSession.ENTRY_WINDOW
    assert _session(intraday, at_1100) is NseSession.OPEN_NO_ENTRY
    assert _session(swing, at_1100) is NseSession.ENTRY_WINDOW


# ---------------------------------------------------------------------------
# Multi-scanner set (Decision 026 + 029)
# ---------------------------------------------------------------------------
def test_both_sets_active_and_universes_disjoint() -> None:
    """Broad set taken live (user decision 2026-07-27): both sets now trade.

    The validated NIFTY-100 set (``gap_down_reversal``, default scanner) and
    the broad Midcap150+Smallcap100 set (``gap_down_reversal_broad``, ``broad``
    scanner) are BOTH active in strategy_master.csv. The two universes MUST
    stay disjoint: dedup is per (bucket, strategy, symbol) and the sets run as
    different strategies, so an overlapping name could be entered twice (once
    per set) within one tick. Disjoint universes are what prevent that.
    """
    b = load_bucket("intraday-indian")
    master = load_strategy_master(
        b.strategy_master_csv_path, b.trading_type.value
    )
    assert {(r.strategy_name, r.scanner) for r in master.rows} == {
        ("gap_down_reversal", ""),
        ("gap_down_reversal_broad", "broad"),
    }, "both the validated and broad sets should be active"
    # The two scanned universes must never overlap (double-entry guard).
    narrow = set(load_scanner_config(b.scanner_yaml_path_for("")).symbols)
    broad = set(load_scanner_config(b.scanner_yaml_path_for("broad")).symbols)
    assert not (narrow & broad), sorted(narrow & broad)[:5]


def test_broad_set_shares_the_strategy_logic() -> None:
    """The broad set must be the SAME strategy, so fixes can't diverge."""
    assert issubclass(GapDownReversalBroad, GapDownReversal)
    assert GapDownReversalBroad.tf == GapDownReversal.tf
    assert GapDownReversalBroad.name != GapDownReversal.name


def test_only_the_broad_set_screens_circuit_bands() -> None:
    """NIFTY-100 is ~all F&O, so a band filter there would be noise."""
    b = load_bucket("intraday-indian")
    narrow = gr.GapReversalConfig.from_scanner_config(
        load_scanner_config(b.scanner_yaml_path_for(""))
    )
    broad = gr.GapReversalConfig.from_scanner_config(
        load_scanner_config(b.scanner_yaml_path_for("broad"))
    )
    assert narrow.min_circuit_band_pct == 0
    assert broad.min_circuit_band_pct == Decimal("20.0")
    # Signal thresholds must stay identical — only the universe differs.
    assert broad.gap_min_pct == narrow.gap_min_pct
    assert broad.gap_max_pct == narrow.gap_max_pct
    assert broad.first15_body_atr_frac == narrow.first15_body_atr_frac


# ---------------------------------------------------------------------------
# Shared capital budget across scanner sets
# ---------------------------------------------------------------------------
def test_bucket_capital_supports_exactly_five_slots() -> None:
    """20% cap × 5 = 100% of capital; the two sets share those 5 slots."""
    b = load_bucket("intraday-indian")
    alloc = load_allocator_config(b.allocator_yaml_path)
    per_trade_margin = b.config.capital_inr * alloc.per_symbol_cap
    assert per_trade_margin == Decimal("10000")
    assert per_trade_margin * b.config.leverage_max == Decimal("50000")
    assert alloc.per_symbol_cap * 5 == alloc.aggregate_cap


# ---------------------------------------------------------------------------
# Product routing (MIS)
# ---------------------------------------------------------------------------
def test_intraday_routes_mis_and_swing_routes_mtf() -> None:
    assert load_bucket("intraday-indian").config.product == "INTRADAY"
    assert load_bucket("swing-indian").config.product == "MTF"


def test_order_request_carries_product() -> None:
    req = OrderRequest(
        symbol="X", side="buy", size=Decimal("1"), product="INTRADAY"
    )
    assert req.product == "INTRADAY"
    assert OrderRequest(symbol="X", side="buy", size=Decimal("1")).product is None


# ---------------------------------------------------------------------------
# Per-bucket P&L ledger (shared Dhan wallet)
# ---------------------------------------------------------------------------
def test_ledger_pnl_is_independent_of_the_shared_wallet() -> None:
    """Two Indian buckets on one Dhan account must not report each other's P&L.

    The wallet-mirror form double-counts (the reconciler warns about exactly
    this); the ledger form is built from the bucket's own trades.
    """
    shared_wallet = Decimal("1000000")  # Dhan sandbox: same figure for both
    wallet_a, _ = bucket_cumulative_pnl(
        capital=Decimal("50000"), available=shared_wallet, locked=Decimal("0")
    )
    wallet_b, _ = bucket_cumulative_pnl(
        capital=Decimal("50000"), available=shared_wallet, locked=Decimal("0")
    )
    assert wallet_a == wallet_b == Decimal("950000")  # both wrong, identically

    ledger_a, pct_a = bucket_ledger_pnl(
        capital=Decimal("50000"), realized=Decimal("1200"), unrealized=Decimal("-300")
    )
    ledger_b, _ = bucket_ledger_pnl(
        capital=Decimal("50000"), realized=Decimal("0"), unrealized=Decimal("0")
    )
    assert ledger_a == Decimal("900")
    assert ledger_b == Decimal("0")
    assert pct_a == Decimal("1.8")


# ---------------------------------------------------------------------------
# Per-scrip leverage (Decision 030)
# ---------------------------------------------------------------------------
class _LevFeed(_Feed):
    """Feed that also answers the scrip-master leverage lookup."""

    def __init__(self, lev: Decimal | None) -> None:
        super().__init__([])
        self._lev = lev

    def max_leverage(self, symbol: str) -> Decimal | None:  # noqa: ARG002
        return self._lev


class _NoPreflightBroker:
    """Broker whose venue offers no margin preflight."""

    def required_margin(self, *a: object, **k: object) -> Decimal | None:  # noqa: ARG002
        return None


class _PricedBroker:
    """Broker that prices the order at a fixed per-unit margin."""

    def __init__(self, per_unit: Decimal) -> None:
        self._per_unit = per_unit

    def required_margin(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal, product=None
    ) -> Decimal | None:  # noqa: ARG002
        return quantity * self._per_unit


def _runner_with(data: object) -> BucketRunner:
    r = object.__new__(BucketRunner)
    r.bucket = load_bucket("intraday-indian")
    r._data = data
    return r


def test_sizes_on_scrip_leverage_when_no_preflight() -> None:
    """A 3x scrip must be sized at 3x, not the bucket's 5x ceiling nor 1x."""
    r = _runner_with(_LevFeed(Decimal("3")))
    # ₹10k margin at 3x = ₹30k notional; at ₹100/share that is 300 shares.
    fitted = r._fit_to_margin(
        broker=_NoPreflightBroker(),
        symbol="X",
        side="buy",
        size=Decimal("500"),          # what 5x would have asked for
        price=Decimal("100"),
        margin_budget=Decimal("10000"),
    )
    assert fitted == Decimal("300")


def test_scrip_leverage_is_capped_by_the_bucket_ceiling() -> None:
    """A scrip allowing 10x must still be traded at the bucket's 5x."""
    r = _runner_with(_LevFeed(Decimal("10")))
    fitted = r._fit_to_margin(
        broker=_NoPreflightBroker(),
        symbol="X",
        side="buy",
        size=Decimal("9999"),
        price=Decimal("100"),
        margin_budget=Decimal("10000"),
    )
    assert fitted == Decimal("500")  # 10k x 5 / 100


def test_unknown_scrip_leverage_falls_back_to_1x() -> None:
    r = _runner_with(_LevFeed(None))
    fitted = r._fit_to_margin(
        broker=_NoPreflightBroker(),
        symbol="X",
        side="buy",
        size=Decimal("500"),
        price=Decimal("100"),
        margin_budget=Decimal("10000"),
    )
    assert fitted == Decimal("100")  # 10k x 1 / 100


def test_venue_preflight_overrides_the_scrip_estimate() -> None:
    """When the venue prices the order, that figure wins."""
    r = _runner_with(_LevFeed(Decimal("5")))
    # Venue says 40/unit; budget 10k affords 250 units, not the 500 requested.
    fitted = r._fit_to_margin(
        broker=_PricedBroker(Decimal("40")),
        symbol="X",
        side="buy",
        size=Decimal("500"),
        price=Decimal("100"),
        margin_budget=Decimal("10000"),
    )
    assert fitted == Decimal("250")


def test_preflight_within_budget_leaves_size_untouched() -> None:
    r = _runner_with(_LevFeed(Decimal("5")))
    fitted = r._fit_to_margin(
        broker=_PricedBroker(Decimal("10")),   # 500 x 10 = 5k <= 10k budget
        symbol="X",
        side="buy",
        size=Decimal("500"),
        price=Decimal("100"),
        margin_budget=Decimal("10000"),
    )
    assert fitted == Decimal("500")


def test_margin_fitting_never_touches_crypto_sizes() -> None:
    """Regression: the fit step must not run on crypto buckets.

    ``margin_budget`` is INR and, on crypto, ``price`` is USD per contract —
    dividing them yields a fraction that floors to 0 contracts, which would
    silently stop a LIVE crypto bucket from placing any order at all. Crypto
    sets leverage explicitly per position (Decision 021) and has already done
    its fx/contract-size conversion, so the fit step must be a no-op there.
    """
    r = object.__new__(BucketRunner)
    r.bucket = load_bucket("longterm-crypto")
    r._data = _LevFeed(None)
    fitted = r._fit_to_margin(
        broker=_NoPreflightBroker(),
        symbol="BTCUSD",
        side="buy",
        size=Decimal("3"),              # contracts
        price=Decimal("60000"),         # USD per contract
        margin_budget=Decimal("10000"), # INR
    )
    assert fitted == Decimal("3"), "crypto size must pass through untouched"


# ---------------------------------------------------------------------------
# 1x CNC fallback sizing (Decision 029, amended 2026-07-27)
# ---------------------------------------------------------------------------
def test_one_x_size_is_the_cash_affordable_quantity() -> None:
    """The CNC fallback must buy only what the margin budget covers at 1x.

    This is the guard against the fallback overspending: a quantity sized for
    4x MIS needs 4x the cash as CNC. At ₹100/share a ₹10k slot is 100 shares,
    NOT the 400 that 4x MIS would have bought.
    """
    r = _runner_with(_LevFeed(Decimal("4")))
    assert r._one_x_size(
        price=Decimal("100"), margin_budget=Decimal("10000")
    ) == Decimal("100")


def test_one_x_size_rounds_down_to_whole_shares() -> None:
    r = _runner_with(_LevFeed(None))
    assert r._one_x_size(
        price=Decimal("330"), margin_budget=Decimal("10000")
    ) == Decimal("30")  # 30.3 → 30


def test_one_x_size_none_without_price() -> None:
    r = _runner_with(_LevFeed(None))
    assert r._one_x_size(price=None, margin_budget=Decimal("10000")) is None
    assert r._one_x_size(
        price=Decimal("0"), margin_budget=Decimal("10000")
    ) is None


def test_one_x_size_none_when_bucket_declares_no_fallback() -> None:
    """A bucket without ``fallback_product`` never opts into the retry."""
    r = object.__new__(BucketRunner)
    r.bucket = load_bucket("longterm-indian")  # no fallback_product configured
    r._data = _LevFeed(None)
    assert r.bucket.config.fallback_product is None
    assert r._one_x_size(
        price=Decimal("100"), margin_budget=Decimal("10000")
    ) is None


def test_swing_bucket_declares_cnc_fallback_so_mtf_retry_is_capped() -> None:
    """MTF→CNC must be size-capped, not a full-notional cash order.

    Without ``fallback_product`` the runner passes no ``fallback_max_size``, and
    the Dhan client's MTF retry would re-send the LEVERAGED quantity as cash —
    ~3.8× the margin the sizer budgeted, out of an account shared with the
    user's own money (Decision 032).
    """
    r = object.__new__(BucketRunner)
    r.bucket = load_bucket("swing-indian")
    r._data = _LevFeed(None)
    assert r.bucket.config.fallback_product == "CNC"
    assert r._one_x_size(
        price=Decimal("100"), margin_budget=Decimal("10000")
    ) == Decimal("100")


def test_intraday_bucket_declares_cnc_fallback() -> None:
    assert load_bucket("intraday-indian").config.fallback_product == "CNC"


# ---------------------------------------------------------------------------
# Rejection reasons + metrics (Decision 033)
#
# Before this, ScannerSnapshot stored an empty metrics dict for every rejected
# symbol, so "0/99 gapped down" looked identical whether the market was flat or
# the intraday series was malformed for all 99 names. These assert the screen
# now says which.
# ---------------------------------------------------------------------------
def test_a_rejected_gap_still_reports_the_gap_it_had() -> None:
    """The point of the whole change: -2% is recorded, not discarded as {}."""
    intraday, daily = _screen_inputs(-2.0)
    out = gr.screen_with_reason("TEST", intraday, daily, _DAY, _cfg())
    assert out.candidate is None
    assert out.reason == gr.REASON_GAP_OUT_OF_BAND
    assert round(float(out.metrics["gap_pct"]), 1) == -2.0
    assert out.data_ok  # evaluated fine, simply did not qualify


def test_a_passing_symbol_reports_no_reason_and_full_metrics() -> None:
    intraday, daily = _screen_inputs(-5.0)
    out = gr.screen_with_reason("TEST", intraday, daily, _DAY, _cfg())
    assert out.candidate is not None
    assert out.reason == gr.REASON_OK
    assert {"prev_close", "open_0915", "gap_pct", "body_atr_ratio"} <= set(out.metrics)


def test_a_malformed_open_is_flagged_as_a_data_problem() -> None:
    """This is the case that used to hide behind '0/99 gapped down'."""
    intraday, daily = _screen_inputs(-5.0)
    trimmed = [b for b in intraday if gr.ist_time(b) != time(9, 15)]
    out = gr.screen_with_reason("TEST", trimmed, daily, _DAY, _cfg())
    assert out.candidate is None
    assert not out.data_ok
    assert out.reason.startswith("data_")


def test_missing_daily_history_is_a_data_problem_not_a_no_signal() -> None:
    intraday, _ = _screen_inputs(-5.0)
    out = gr.screen_with_reason("TEST", intraday, [], _DAY, _cfg())
    assert out.reason == gr.REASON_NO_DAILY_CTX
    assert not out.data_ok


def test_weak_body_and_corporate_action_are_evaluable_rejections() -> None:
    """Both looked at real data and declined — not data faults."""
    intraday, daily = _screen_inputs(-5.0, body_frac=0.001)
    weak = gr.screen_with_reason("TEST", intraday, daily, _DAY, _cfg())
    assert weak.reason == gr.REASON_WEAK_BODY
    assert weak.data_ok

    intraday2, _ = _screen_inputs(-5.0)
    corp = gr.screen_with_reason("TEST", intraday2, _daily(30, 50.0), _DAY, _cfg())
    assert corp.reason == gr.REASON_CORP_ACTION
    assert corp.data_ok


def test_gap_screen_wrapper_matches_screen_with_reason_exactly() -> None:
    """The wrapper must not change behaviour for the 4 existing callers."""
    for gap in (-5.0, -2.0, -13.0, +5.0):
        intraday, daily = _screen_inputs(gap)
        wrapped = gr.gap_screen("TEST", intraday, daily, _DAY, _cfg())
        full = gr.screen_with_reason("TEST", intraday, daily, _DAY, _cfg())
        assert wrapped == full.candidate, gap
