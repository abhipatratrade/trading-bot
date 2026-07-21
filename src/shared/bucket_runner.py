"""
BucketRunner — the per-bucket pipeline orchestrator.

Implements the eight-step pipeline from the plan / PPTX slide 3, plus the
exit step added by Decision 021:

    0. Strategy exits (select_exits per strategy — runs even for strategies
       currently blocked by the master/regime gate, since a gated strategy
       must still manage positions it already holds)
    1. Strategy discovery (folder scan)
    2. Strategy Master gate
    3. Market Scanner — one scan per named scanner set (Decision 026);
       each strategy's ``scanner`` column picks its universe AND its
       allocation config (scanner_<name>.yaml + allocator_<name>.yaml)
    4. Regime Selector (Brain)
    5. Regime gate per strategy
    6. Dedup gate (Sizer)
    7. Position Size Allocator (Kelly on live equity, per scanner set)
    8. Order Manager → Broker (safety-wrapped)

One ``BucketRunner`` per (type × market) bucket. ``run_bot`` constructs
one per bucket and ticks them each loop iteration.

CURRENCY NOTE: Phase 1 treats ``bucket.config.capital_inr`` and broker
``mark_price`` in the SAME unit (whatever the broker quotes — USD for
Delta India; INR for Dhan when that arrives). The ``_inr`` suffix on
schema fields matches the original PPTX framing of "₹50k per bucket"
but the runtime just sees abstract numbers. A proper FX layer ships in
Phase 3 when Indian buckets go live.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.brokers.base import Broker, OrderType
from src.core.alerts import send_alert_dedup
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    MarketRegime,
    OrderSide,
    OrderStatus,
    Position,
    PositionSide,
    SizingDecision,
    Trade,
)
from src.data_sources.base import MarketData
from src.order_manager.manager import KillSwitchEngagedError, OrderManager
from src.safety import kill_switch
from src.shared.allocator.sizer import (
    AllocatorConfig,
    dedup_window_hours_for_tf,
    load_allocator_config,
    size_positions,
)
from src.shared.base_strategy import Strategy
from src.shared.bucket import Bucket, Market
from src.shared.market_calendar import NseSession, nse_session, parse_ist_time
from src.shared.regime.brain import RegimeConfig, load_regime_config, predict_regime
from src.shared.regime.store import MARKET_SENTINEL
from src.shared.scanner.engine import (
    ScannerConfig,
    ScanResult,
    load_scanner_config,
    run_scan,
)
from src.shared.strategy_loader import discover_strategies
from src.shared.strategy_master.loader import (
    StrategyMaster,
    load_strategy_master,
)

_log = get_logger("shared.bucket_runner")

_TF_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def tick_interval_for_tf(tf: str) -> int:
    """Seconds between full pipeline passes for a bucket at timeframe ``tf``.

    A 1d bucket gains nothing from re-scanning every 60s — signals only
    change on bar close. Interval = tf/20 clamped to [60, 900]:
    5m/15m → 60s, 1h → 180s, 4h → 720s, 1d → 900s (15 min). Safety paths
    (breakers, stop sweep, kill switch) are NOT affected — they run on
    the main 60s loop in ``run_bot``.

    Unparseable tf → 60 (legacy every-tick behaviour, fail-fast is the
    loader's job).
    """
    try:
        unit = tf[-1].lower()
        value = int(tf[:-1])
        tf_seconds = value * _TF_UNIT_SECONDS[unit]
    except (KeyError, ValueError, IndexError):
        return 60
    return max(60, min(900, tf_seconds // 20))


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Result of ``BucketRunner.run_once``."""

    bucket_id: str
    placed: int
    skipped: dict[SizingDecision, int]
    eligible_strategies: list[str]
    blocked_strategies: dict[str, str]
    universe: list[str]
    regime: MarketRegime | None
    exited: int = 0


class BucketRunner:
    """One pipeline per bucket. Configs loaded eagerly on construction."""

    def __init__(
        self,
        *,
        bucket: Bucket,
        brokers: Mapping[str, Broker],
        data: MarketData,
        order_managers: Mapping[str, OrderManager],
        clock: Clock | None = None,
    ) -> None:
        self.bucket = bucket
        self._brokers = brokers
        self._data = data
        self._oms = order_managers
        self._clock = clock or RealClock()

        # Fail-fast on bad config (Decision 006).
        self.master: StrategyMaster = load_strategy_master(
            bucket.strategy_master_csv_path,
            bucket_trading_type=bucket.trading_type.value,
        )
        self.regime_config: RegimeConfig = load_regime_config(
            bucket.regime_yaml_path
        )
        # Decision 026 — one scanner/allocator config pair per named
        # scanner set used in strategy_master.csv ("" = the default
        # scanner.yaml + allocator.yaml, always loaded). A named set with
        # missing yaml files fails the boot, not the tick.
        scanner_names = {""} | {row.scanner for row in self.master.rows}
        self.scanner_configs: dict[str, ScannerConfig] = {
            name: load_scanner_config(bucket.scanner_yaml_path_for(name))
            for name in scanner_names
        }
        self.allocator_configs: dict[str, AllocatorConfig] = {
            name: load_allocator_config(bucket.allocator_yaml_path_for(name))
            for name in scanner_names
        }
        # Default-pair aliases kept for external readers (run_bot fx map,
        # stop-protection fallbacks, tests).
        self.scanner_config: ScannerConfig = self.scanner_configs[""]
        self.allocator_config: AllocatorConfig = self.allocator_configs[""]
        self.strategies: dict[str, type[Strategy]] = discover_strategies(
            bucket.strategies_folder
        )
        # Full pipeline passes are paced to the bucket's timeframe; the
        # 60s main loop skips this runner until the interval elapses.
        self.tick_interval_seconds: int = tick_interval_for_tf(
            self.regime_config.tf
        )

    # ── Main entry ─────────────────────────────────────────────────────
    def run_once(self) -> RunSummary:
        """Execute one full pipeline pass for this bucket."""
        _log.info("bucket_run_start", bucket_id=self.bucket.id)

        if not self.bucket.config.enabled:
            _log.info("bucket_disabled_skip", bucket_id=self.bucket.id)
            return _empty_summary(self.bucket.id)

        account_ref = self.bucket.config.account_ref
        broker = self._brokers.get(account_ref)
        order_manager = self._oms.get(account_ref)
        if broker is None or order_manager is None:
            _log.warning(
                "bucket_broker_unavailable",
                bucket_id=self.bucket.id,
                wanted=account_ref,
            )
            return _empty_summary(self.bucket.id)

        # Market-hours gate (equity buckets only; crypto is 24/7). When the NSE
        # session is CLOSED there is nothing to do — we can't even place exits —
        # so skip the whole pass. OPEN_NO_ENTRY runs exits but no new entries.
        session_state = self._equity_session_state()
        if session_state is NseSession.CLOSED:
            _log.info("bucket_market_closed", bucket_id=self.bucket.id)
            return _empty_summary(self.bucket.id)

        # 0. Exits — strategy-driven closes run before any entry logic
        # (Decision 021). A failed exit must not block other strategies.
        # Decision 024: exits also run while the kill switch is engaged —
        # they are reduce-only, and a halted bucket must still manage the
        # positions it holds. Entries below are what the kill blocks.
        exited = self._run_exits(order_manager)

        if kill_switch.is_engaged(self.bucket.id):
            _log.info(
                "bucket_killed_exits_only",
                bucket_id=self.bucket.id,
                exited=exited,
            )
            return RunSummary(
                bucket_id=self.bucket.id,
                placed=0,
                skipped={},
                eligible_strategies=[],
                blocked_strategies={"*": "kill switch engaged"},
                universe=[],
                regime=None,
                exited=exited,
            )

        # Entry-window gate: session is open but, for an equity bucket outside
        # the morning entry window, we manage exits only — never open a position
        # on a gap that has gone stale since the 09:45 open.
        if session_state is not NseSession.ENTRY_WINDOW:
            _log.info(
                "bucket_exits_only_outside_entry_window",
                bucket_id=self.bucket.id,
                exited=exited,
            )
            return RunSummary(
                bucket_id=self.bucket.id,
                placed=0,
                skipped={},
                eligible_strategies=[],
                blocked_strategies={"*": f"market: {session_state.value}"},
                universe=[],
                regime=None,
                exited=exited,
            )

        # 1+2: discovery + master gate (already loaded; gate is per-strategy below).

        # 3. Scanner
        # One scan per named scanner set, run lazily on first use this
        # tick (Decision 026). Named scans persist their snapshots under
        # "<bucket_id>:<scanner>" so they don't collide with the default
        # scan's (date, strategy_id, symbol) unique key.
        scans: dict[str, ScanResult] = {}

        def _scan_for(name: str) -> ScanResult:
            if name not in scans:
                scans[name] = run_scan(
                    bucket_id=(
                        f"{self.bucket.id}:{name}" if name else self.bucket.id
                    ),
                    data=self._data,
                    config=self.scanner_configs[name],
                    scan_date=self._clock.now().date(),
                    require_binance_listed=(
                        self.bucket.market.value == "crypto"
                    ),
                )
            return scans[name]

        _scan_for("")  # default scan always runs (universe rows + dashboards)

        # 4. Regime
        # Broad-market label drives the per-strategy regime gate
        # (whether the strategy concept applies in the current market).
        market_pred = predict_regime(
            bucket_id=self.bucket.id,
            symbol=MARKET_SENTINEL,
            config=self.regime_config,
            data=self._data,
            clock=self._clock,
        )
        market_regime: MarketRegime | None = (
            market_pred.regime if market_pred else None
        )

        # 5+6+7: per-strategy gate, dedup (in sizer), Kelly
        # Per-symbol regimes are computed lazily inside the per-strategy
        # loop only for the candidates that actually enter the sizer —
        # avoids predicting for symbols the strategy doesn't want anyway.
        placed = 0
        skipped_counts: dict[SizingDecision, int] = {}
        eligible: list[str] = []
        blocked: dict[str, str] = {}
        # Margin claimed so far THIS TICK across every strategy in this bucket.
        # Strategies are iterated in sorted filename order, so the claim order
        # is deterministic — and for intraday-indian it usefully favours the
        # validated NIFTY-100 set (``gap_down_reversal``) over the broader
        # experimental one (``gap_down_reversal_broad``) when both signal on
        # the same bar and there are not enough slots for both.
        committed_margin = Decimal("0")

        for strat_name, strat_cls in self.strategies.items():
            row = self.master.by_name.get(strat_name)
            if row is None:
                blocked[strat_name] = "not in strategy_master.csv"
                _log_strategy_blocked(
                    self.bucket.id, strat_name, blocked[strat_name]
                )
                continue

            if market_regime is not None and not row.passes_regime_gate(
                market_regime
            ):
                blocked[strat_name] = f"regime gate: market={market_regime.value}"
                _log_strategy_blocked(
                    self.bucket.id, strat_name, blocked[strat_name]
                )
                continue

            eligible.append(strat_name)
            strategy = strat_cls()
            strat_scan = _scan_for(row.scanner)
            entry_candidates = strategy.select_entries(
                strat_scan.universe, self._data
            )
            if not entry_candidates:
                continue

            symbols = [ec.symbol for ec in entry_candidates]
            # Honor the strategy's declared direction (Decision 021: some
            # strategies are long-only, others long/short).
            sides = {ec.symbol: ec.side for ec in entry_candidates}
            mark_prices = self._collect_mark_prices(symbols)

            # Per-symbol regimes for the sizer (one HMM call per
            # candidate; the brain caches per inference window so
            # subsequent ticks within the same bar are no-ops).
            regimes: dict[str, MarketRegime | None] = {}
            for sym in symbols:
                pred = predict_regime(
                    bucket_id=self.bucket.id,
                    symbol=sym,
                    config=self.regime_config,
                    data=self._data,
                    clock=self._clock,
                )
                regimes[sym] = pred.regime if pred else None

            # Live contract sizes from the broker's product catalogue
            # (None ⇒ symbol unknown, YAML table applies). FX stays the
            # fixed allocator.yaml rate — user decision 2026-07-07:
            # 1 USD = 85 INR for Delta India, no live feed.
            live_contract_sizes: dict[str, Decimal] = {}
            for sym in symbols:
                cs = broker.contract_size(sym, default=None)
                if cs is not None:
                    live_contract_sizes[sym] = cs

            results = size_positions(
                bucket=self.bucket,
                strategy_name=strat_name,
                candidates=symbols,
                mark_prices_inr=mark_prices,
                regimes=regimes,
                # Decision 026: the strategy's scanner set carries its own
                # allocation logic (μ/σ, Kelly fraction, caps, regime
                # multipliers, fx).
                config=self.allocator_configs[row.scanner],
                # One-bar re-entry lockout at the STRATEGY's timeframe
                # (1d → 23h, 1h → ~57 min), not a hardcoded 23h.
                dedup_window_hours=dedup_window_hours_for_tf(row.tf),
                contract_sizes_override=live_contract_sizes,
                # Decision 026 + 029: strategies in this bucket share ONE
                # capital pool, but each has its own allocator config and
                # they all read the same (sweep-stale) bucket_state. Pass
                # what earlier strategies already claimed this tick so slots
                # go first-come-first-served instead of every scanner set
                # independently claiming the whole bucket.
                committed_margin_inr=committed_margin,
            )

            for sym, res in results.items():
                if res.decision == SizingDecision.PLACED:
                    committed_margin += res.required_margin_inr
                    size = self._fit_to_margin(
                        broker=broker,
                        symbol=sym,
                        side=sides.get(sym, "buy"),
                        size=res.contracts,
                        price=mark_prices.get(sym),
                        margin_budget=res.required_margin_inr,
                    )
                    if size < 1:
                        _log.warning(
                            "open_skipped_margin_unaffordable",
                            bucket_id=self.bucket.id,
                            symbol=sym,
                            margin_budget=str(res.required_margin_inr),
                        )
                        continue
                    self._place_order(
                        broker=broker,
                        om=order_manager,
                        strat_name=strat_name,
                        symbol=sym,
                        side=sides.get(sym, "buy"),
                        size=size,
                    )
                    placed += 1
                else:
                    skipped_counts[res.decision] = (
                        skipped_counts.get(res.decision, 0) + 1
                    )

        _log.info(
            "bucket_run_complete",
            bucket_id=self.bucket.id,
            placed=placed,
            exited=exited,
            eligible=eligible,
            blocked=list(blocked),
        )

        return RunSummary(
            bucket_id=self.bucket.id,
            placed=placed,
            skipped=skipped_counts,
            eligible_strategies=eligible,
            blocked_strategies=blocked,
            # Union across every scanner set that ran this tick.
            universe=sorted({s for r in scans.values() for s in r.universe}),
            regime=market_regime,
            exited=exited,
        )

    # ── Internals ──────────────────────────────────────────────────────
    def _equity_session_state(self) -> NseSession:
        """NSE session state for this bucket's market.

        Crypto is 24/7, so it is always in the entry window (path unchanged).
        Indian buckets defer to the NSE calendar (hours + holidays).
        """
        if self.bucket.market != Market.INDIAN:
            return NseSession.ENTRY_WINDOW
        return nse_session(
            self._clock.now(),
            entry_start=parse_ist_time(self.bucket.config.entry_start),
            entry_end=parse_ist_time(self.bucket.config.entry_end),
        )

    def _run_exits(self, om: OrderManager) -> int:
        """Step 0: ask every discovered strategy which held positions to close.

        Runs for ALL strategies with open positions in this bucket — the
        master/regime gate does not apply to exits (a gated strategy still
        manages what it holds). Exit orders are reduce-only market orders;
        the Position row is flipped FLAT optimistically and the reconciler
        re-imports it if the close somehow didn't stick.
        """
        with session_scope() as session:
            held_rows = list(
                session.execute(
                    select(Position).where(
                        Position.bucket_id == self.bucket.id,
                        Position.side != PositionSide.FLAT,
                        Position.quantity > 0,
                    )
                ).scalars()
            )
            # Belt-and-braces: skip symbols with an active reduce-only Trade
            # placed recently (exit already in flight, position row not yet
            # reconciled FLAT).
            cutoff = self._clock.now() - timedelta(hours=1)
            recent_trades = list(
                session.execute(
                    select(Trade).where(
                        Trade.bucket_id == self.bucket.id,
                        Trade.status.in_(
                            [
                                OrderStatus.PENDING,
                                OrderStatus.OPEN,
                                OrderStatus.PARTIAL,
                                OrderStatus.FILLED,
                            ]
                        ),
                        Trade.created_at > cutoff,
                    )
                ).scalars()
            )
        recent_exit_keys = {
            (t.strategy_name, t.symbol)
            for t in recent_trades
            if t.extra
            and t.extra.get("reduce_only")
            # A resting protective stop (Decision 022) is not an exit in
            # flight — it must not suppress a strategy-driven close.
            and not t.extra.get("protective_stop")
        }

        if not held_rows:
            return 0

        by_strategy: dict[str, dict[str, Position]] = {}
        for pos in held_rows:
            if not pos.strategy_name:
                _log.warning(
                    "held_position_without_strategy_name",
                    bucket_id=self.bucket.id,
                    symbol=pos.symbol,
                )
                continue
            by_strategy.setdefault(pos.strategy_name, {})[pos.symbol] = pos

        exited = 0
        for strat_name, held in by_strategy.items():
            strat_cls = self.strategies.get(strat_name)
            if strat_cls is None:
                send_alert_dedup(
                    f"exit_no_strategy:{self.bucket.id}:{strat_name}",
                    f"[{self.bucket.id}] positions held by unknown strategy "
                    f"{strat_name!r} ({list(held)}) — no exit logic running",
                )
                continue

            regimes: dict[str, MarketRegime | None] = {}
            for sym in held:
                pred = predict_regime(
                    bucket_id=self.bucket.id,
                    symbol=sym,
                    config=self.regime_config,
                    data=self._data,
                    clock=self._clock,
                )
                regimes[sym] = pred.regime if pred else None

            try:
                exit_symbols = strat_cls().select_exits(held, self._data, regimes)
            except Exception:
                _log.error(
                    "select_exits_failed",
                    bucket_id=self.bucket.id,
                    strategy=strat_name,
                    exc_info=True,
                )
                continue

            for sym in exit_symbols:
                pos = held.get(sym)
                if pos is None or (strat_name, sym) in recent_exit_keys:
                    continue
                if self._close_position(om, strat_name, pos, regimes.get(sym)):
                    exited += 1
        return exited

    def _close_position(
        self,
        om: OrderManager,
        strat_name: str,
        pos: Position,
        regime: MarketRegime | None,
    ) -> bool:
        exit_side = (
            OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
        )
        try:
            om.place_order(
                strategy_id=self.bucket.id,
                bucket_id=self.bucket.id,
                strategy_name=strat_name,
                symbol=pos.symbol,
                side=exit_side.value,
                size=pos.quantity,
                order_type=OrderType.MARKET,
                reduce_only=True,
                # Decision 024: strategy exits are risk-reducing and pass
                # an engaged kill switch (same as breaker flatten / stops).
                allow_when_killed=True,
                intent_id=f"exit-{self._clock.now().strftime('%Y%m%d%H%M')}",
            )
        except KillSwitchEngagedError:
            raise
        except Exception:
            _log.error(
                "close_position_failed",
                bucket_id=self.bucket.id,
                symbol=pos.symbol,
                exc_info=True,
            )
            send_alert_dedup(
                f"exit_failed:{self.bucket.id}:{pos.symbol}",
                f"[{self.bucket.id}] FAILED to close {pos.symbol} via {strat_name}",
            )
            return False

        # Optimistic close: reconciler re-imports if the exchange disagrees.
        with session_scope() as session:
            row = session.get(Position, pos.id)
            if row is not None:
                row.side = PositionSide.FLAT
                row.quantity = Decimal("0")
                row.closed_at = self._clock.now()
            session.add(
                AuditLog(
                    strategy_id=self.bucket.id,
                    event_type=AuditEventType.POSITION_CLOSED,
                    message=(
                        f"{strat_name} exit {pos.symbol} "
                        f"({pos.side.value} {pos.quantity})"
                    ),
                    payload={
                        "bucket_id": self.bucket.id,
                        "strategy_name": strat_name,
                        "symbol": pos.symbol,
                        "position_side": pos.side.value,
                        "quantity": str(pos.quantity),
                        "regime": regime.value if regime else None,
                    },
                )
            )
        return True

    def _collect_mark_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for s in symbols:
            try:
                t = self._data.get_ticker(s)
            except Exception:
                _log.warning("mark_price_fetch_failed", symbol=s, exc_info=True)
                continue
            price = t.mark_price or t.last_price
            if price and price > 0:
                out[s] = price
        return out

    def _fit_to_margin(
        self,
        *,
        broker: Broker,
        symbol: str,
        side: str,
        size: Decimal,
        price: Decimal | None,
        margin_budget: Decimal,
    ) -> Decimal:
        """Fit ``size`` to the leverage this scrip is ACTUALLY granted.

        ``leverage_max`` is the bucket's risk CEILING, not a promise. NSE cash
        leverage is graded per scrip: measured 2026-07-21, the median is 4.44x
        across NIFTY-100, 3.79x across Midcap 150 and 3.06x across Smallcap
        100 — and no name in any of them reaches 5x. Sizing everything at 5x
        would over-order on effectively every trade and collect an RMS
        rejection (Decision 030).

        Two sources, in order of authority:

        1. ``Broker.required_margin`` — the venue prices this exact order.
           Scaling to it deploys the full margin budget at whatever multiple
           is truly allowed, which IS "trade at max allowed leverage".
        2. The scrip master's per-scrip figure (``MarketData.max_leverage``),
           capped at the bucket ceiling. Used when the venue offers no
           preflight.

        If neither is available, size at 1x — the only quantity guaranteed
        affordable whatever the broker grants. Undersized beats rejected, and
        beats accidentally over-levered by a wide margin.
        """
        if price is None or price <= 0 or size <= 0:
            return size
        needed = broker.required_margin(
            symbol, side, size, price, product=self.bucket.config.product
        )
        if needed is None:
            # Fall back to the scrip's own ceiling, capped by the bucket's.
            scrip_lev = None
            if hasattr(self._data, "max_leverage"):
                scrip_lev = self._data.max_leverage(symbol)
            lev = (
                min(scrip_lev, self.bucket.config.leverage_max)
                if scrip_lev is not None
                else Decimal("1")
            )
            affordable = margin_budget * lev / price
            fitted = min(size, affordable).to_integral_value(rounding="ROUND_DOWN")
            if fitted < size:
                _log.warning(
                    "margin_preflight_unavailable_sized_on_scrip_leverage",
                    bucket_id=self.bucket.id,
                    symbol=symbol,
                    wanted=str(size),
                    fitted=str(fitted),
                    leverage_used=str(lev),
                    scrip_leverage=str(scrip_lev) if scrip_lev else "unknown",
                )
            return fitted
        if needed <= margin_budget:
            return size
        scaled = (size * margin_budget / needed).to_integral_value(
            rounding="ROUND_DOWN"
        )
        _log.warning(
            "position_resized_to_granted_margin",
            bucket_id=self.bucket.id,
            symbol=symbol,
            wanted=str(size),
            fitted=str(scaled),
            margin_needed=str(needed),
            margin_budget=str(margin_budget),
        )
        return scaled

    def _place_order(
        self,
        *,
        broker: Broker,
        om: OrderManager,
        strat_name: str,
        symbol: str,
        side: str,
        size: Decimal,
    ) -> None:
        try:
            om.place_order(
                strategy_id=self.bucket.id,
                bucket_id=self.bucket.id,
                strategy_name=strat_name,
                symbol=symbol,
                side=side,
                size=size,
                order_type=OrderType.MARKET,
                leverage=self.bucket.config.leverage_max,
                product=self.bucket.config.product,
                intent_id=f"open-{self._clock.now().strftime('%Y%m%d%H%M')}",
            )
        except KillSwitchEngagedError:
            _log.warning(
                "open_blocked_kill_switch",
                bucket_id=self.bucket.id,
                symbol=symbol,
            )
            raise
        except Exception:
            _log.error(
                "open_position_failed",
                bucket_id=self.bucket.id,
                symbol=symbol,
                exc_info=True,
            )
            send_alert_dedup(
                f"open_failed:{self.bucket.id}:{symbol}",
                f"[{self.bucket.id}] FAILED to open {symbol} via {strat_name}",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _empty_summary(bucket_id: str) -> RunSummary:
    return RunSummary(
        bucket_id=bucket_id,
        placed=0,
        skipped={},
        eligible_strategies=[],
        blocked_strategies={},
        universe=[],
        regime=None,
    )


def _log_strategy_blocked(bucket_id: str, strat_name: str, reason: str) -> None:
    with session_scope() as session:
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.STRATEGY_GATE_BLOCKED,
                message=f"{strat_name} blocked: {reason}",
                payload={
                    "bucket_id": bucket_id,
                    "strategy_name": strat_name,
                    "reason": reason,
                },
            )
        )
