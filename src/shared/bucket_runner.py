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
from src.core.config import get_settings
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
from src.safety.stop_protection import resolve_stop_trigger, resolve_target_price
from src.shared.allocator.sizer import (
    AllocatorConfig,
    dedup_window_hours_for_tf,
    load_allocator_config,
    quantize_to_lots,
    size_positions,
)
from src.shared.base_strategy import Strategy
from src.shared.bucket import Bucket, Market
from src.shared.contract_selection import (
    ContractSelectionConfig,
    ContractSelector,
    Selection,
    contract_hint,
    load_contract_selection,
    plan_roll,
)
from src.shared.contracts import is_derivative, underlying_of
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


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """What each candidate underlying actually trades (Decision 036, Phase C).

    Every mapping is keyed by the UNDERLYING, because that is what the scanner
    produced, what the regime model labelled, and what the sizer dedups on. The
    values carry the CONTRACT. For a bucket with no ``contracts.yaml`` the two
    are the same symbol and this is a pass-through, which is why the runner has
    no F&O branch in its hot loop.
    """

    symbols: list[str]
    exec_symbols: dict[str, str]
    exec_prices: dict[str, Decimal]
    # ORDER QUANTITY per lot — what goes in the order's quantity field.
    lot_sizes: dict[str, Decimal]
    # UNDERLYING UNITS per lot — what notional and margin are computed on.
    #
    # Equal to lot_size on NSE and NOT equal on MCX, where the master reports
    # LOT_SIZE 1 while a Natural Gas Mini lot controls 250 mmBtu. Conflating
    # them sizes an MCX position 250x wrong, so the two are carried separately
    # even though every NSE bucket has them identical.
    multipliers: dict[str, Decimal]
    contract_hints: dict[str, dict[str, object]]


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
        # Decision 036 — how a signal on an underlying becomes one contract.
        # OPTIONAL: absent for every cash-equity and crypto bucket, which trade
        # the symbol the scanner produced. Loaded eagerly like everything else
        # so a malformed rule fails the boot rather than the tick.
        self.contract_configs: dict[str, ContractSelectionConfig | None] = {
            name: (
                load_contract_selection(bucket.contracts_yaml_path_for(name))
                if bucket.contracts_yaml_path_for(name).is_file()
                else None
            )
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
        #
        # The pace is the FASTEST thing in the bucket, not the regime's own TF:
        # swing-indian keeps a 1d regime model while running a 1h strategy, and
        # pacing that bucket at the 1d interval (900s) would act on a 1h close
        # up to fifteen minutes late. ``tick_interval_seconds`` in buckets.yaml
        # overrides both when a bucket needs a specific cadence.
        tfs = [self.regime_config.tf] + [row.tf for row in self.master.rows]
        self.tick_interval_seconds: int = (
            bucket.config.tick_interval_seconds
            if bucket.config.tick_interval_seconds is not None
            else min(tick_interval_for_tf(tf) for tf in tfs)
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

        # Decision 037 — carry the contract forward. Before the kill-switch
        # gate, because closing an expiring contract is risk-REDUCING and a
        # position left to expire is worse than a halted bucket; the reopen
        # half checks the switch itself.
        self._roll_expiring(order_manager)

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
                    now=self._clock.now(),
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
            hints = {ec.symbol: ec.hint for ec in entry_candidates}
            price_fetch_failed: set[str] = set()
            mark_prices = self._collect_mark_prices(symbols, price_fetch_failed)

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

            # Decision 036 — the signal and the instrument stop being the
            # same thing. For a bucket with a contracts.yaml, each underlying
            # resolves to one contract here; ``exec_*`` then carries the
            # CONTRACT while ``symbols`` and ``mark_prices`` keep carrying the
            # UNDERLYING, which is what the sizer's dedup and the regime model
            # reason about. For every other bucket these are the same objects,
            # so there is no second code path to keep in step.
            plan = self._resolve_contracts(
                scanner=row.scanner,
                symbols=symbols,
                sides=sides,
                spot_prices=mark_prices,
            )
            symbols = plan.symbols
            if not symbols:
                continue

            # Live contract sizes from the broker's product catalogue
            # (None ⇒ symbol unknown, YAML table applies). FX stays the
            # fixed allocator.yaml rate — user decision 2026-07-07:
            # 1 USD = 85 INR for Delta India, no live feed.
            #
            # For a derivative the lot IS the contract size, so the sizer
            # counts LOTS and the existing ``size < 1`` guard downstream
            # becomes "less than one lot" for free. Keyed by UNDERLYING
            # because that is what the sizer iterates.
            # The sizer divides a target notional by ``price x contract_size``
            # to count LOTS, so the value it needs is the MULTIPLIER (underlying
            # units per lot), not the order-quantity unit. Identical on NSE;
            # 250x apart on MCX gas.
            live_contract_sizes: dict[str, Decimal] = dict(plan.multipliers)
            for sym in symbols:
                if sym in live_contract_sizes:
                    continue
                cs = broker.contract_size(sym, default=None)
                if cs is not None:
                    live_contract_sizes[sym] = cs

            results = size_positions(
                bucket=self.bucket,
                strategy_name=strat_name,
                candidates=symbols,
                # The price the position is actually taken at — a contract's
                # premium for F&O, the spot for everything else. Keyed by
                # underlying so the sizer's dedup keeps working unchanged.
                mark_prices_inr=plan.exec_prices,
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
                # So a lost trade is recorded as a lost trade, not as a
                # decision the allocator made.
                price_fetch_failed=price_fetch_failed,
            )

            for sym, res in results.items():
                if res.decision == SizingDecision.PLACED:
                    committed_margin += res.required_margin_inr
                    # The symbol the ORDER goes to. Same as ``sym`` for every
                    # bucket without a contracts.yaml.
                    exec_symbol = plan.exec_symbols.get(sym, sym)
                    exec_price = plan.exec_prices.get(sym)
                    lot_size = plan.lot_sizes.get(sym, Decimal("1"))
                    # The sizer counted LOTS; the venue's order field counts
                    # ORDER UNITS, which is lots x lot_size (and lot_size is 1
                    # on MCX, where one lot IS one unit of order quantity).
                    size = res.contracts * lot_size
                    size = self._fit_to_margin(
                        broker=broker,
                        symbol=exec_symbol,
                        side=sides.get(sym, "buy"),
                        size=size,
                        price=exec_price,
                        margin_budget=res.required_margin_inr,
                    )
                    # Re-quantise: _fit_to_margin scales to the margin actually
                    # granted, and a scaled quantity lands off the lot grid
                    # nearly every time. Never rounds UP — one NIFTY lot is
                    # ~Rs 15.8L of notional against a Rs 5L bucket.
                    size = quantize_to_lots(size, lot_size)
                    if size < 1:
                        _log.warning(
                            "open_skipped_margin_unaffordable",
                            bucket_id=self.bucket.id,
                            symbol=exec_symbol,
                            underlying=sym,
                            lot_size=str(lot_size),
                            margin_budget=str(res.required_margin_inr),
                        )
                        continue
                    self._place_order(
                        broker=broker,
                        om=order_manager,
                        strat_name=strat_name,
                        symbol=exec_symbol,
                        side=sides.get(sym, "buy"),
                        size=size,
                        fallback_max_size=self._one_x_size(
                            price=exec_price,
                            margin_budget=res.required_margin_inr,
                            lot_size=lot_size,
                        ),
                        extra_payload=_entry_extra(
                            hint=hints.get(sym, {}),
                            margin_inr=res.required_margin_inr,
                            decision_price=exec_price,
                            # Which contract, and the spot that chose it —
                            # without both, "why that strike?" is unanswerable
                            # after the fact.
                            contract=plan.contract_hints.get(sym),
                            underlying_price=mark_prices.get(sym),
                        ),
                        # Decision 034: the same two numbers the sweep would
                        # have used AFTER the fill, supplied BEFORE it so the
                        # stop can ride on the entry order itself.
                        mark_price=exec_price,
                        stop_distance=_hint_decimal(
                            hints.get(sym, {}), "stop_distance"
                        ),
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
            # Decision 037 — MCX runs 09:00-23:30 against NSE's 09:15-15:30.
            # Without this a commodity bucket goes dark at 15:30 and never sees
            # the 18:00 IST NYMEX open.
            exchange=self.bucket.config.exchange,
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

        # An Indian equity bucket cannot legitimately hold a SHORT: every
        # strategy here is long-only and a demat account cannot carry one. A
        # short row is therefore corrupt — on 2026-08-18 Dhan reported PIIND
        # short 15 for a few minutes after the position was SOLD (a sale out of
        # holdings shows as a negative day-position until settlement), and that
        # artifact was adopted as a Position row.
        #
        # Exiting it would compute the closing side as BUY and purchase 15
        # shares to "close" a position that does not exist. Exits pass an
        # engaged kill switch by design (Decision 024), so nothing downstream
        # would have stopped it. The reconciler now flattens these rows, but it
        # sweeps on its own 5-minute clock; this refuses to act on one in the
        # window before it does.
        # Decision 037 — ask the BUCKET, not the market. `Market.INDIAN` meant
        # "cash equity" when this guard was written; commodity-indian is also
        # INDIAN and holds shorts as a matter of course, so gating on the
        # market would drop every one of them from the exit engine and leave
        # the position open and unmanaged.
        if not self.bucket.allows_shorts:
            shorts = [p for p in held_rows if p.side == PositionSide.SHORT]
            if shorts:
                for p in shorts:
                    _log.warning(
                        "short_row_ignored_long_only_bucket",
                        bucket_id=self.bucket.id,
                        symbol=p.symbol,
                        quantity=str(p.quantity),
                    )
                held_rows = [
                    p for p in held_rows if p.side != PositionSide.SHORT
                ]
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

    def _roll_expiring(self, om: OrderManager) -> int:
        """Carry open derivative positions into the next contract.

        Decision 037, user instruction 2026-08-29: positions are CARRIED
        FORWARD rather than squared off, and a contract inside the
        ``min_days_to_expiry`` floor is no longer the one to hold.

        A roll is TWO orders and they are NOT symmetrical in risk. The close is
        placed first and the open only follows a confirmed close, because the
        failure modes are not equal: ending up FLAT for one tick costs a
        signal, while ending up DOUBLE — long the near month and the far one at
        once — is twice the exposure the allocator approved, on a bucket whose
        stop cannot fire overnight.

        Runs BEFORE entries and independently of the kill switch's entry block:
        the close half is risk-reducing, and a contract left to expire is a
        worse outcome than a halted bucket. The OPEN half is skipped while
        killed — re-entering is risk-increasing, and Decision 024 is explicit
        that a killed bucket may reduce but never add.
        """
        if not self.bucket.trades_derivatives():
            return 0

        registry = getattr(self._data, "fno", None)
        if registry is None:
            return 0

        with session_scope() as session:
            held = list(
                session.execute(
                    select(Position).where(
                        Position.bucket_id == self.bucket.id,
                        Position.side != PositionSide.FLAT,
                        Position.quantity > 0,
                    )
                ).scalars()
            )
        if not held:
            return 0

        today = self._clock.now().date()
        killed = kill_switch.is_engaged(self.bucket.id)
        rolled = 0

        for pos in held:
            config = self.contract_configs.get("")
            if config is None:
                continue
            decision = plan_roll(
                held_symbol=pos.symbol,
                underlying=underlying_of(pos.symbol),
                source=registry,
                config=config,
                on=today,
            )
            if not decision.should_roll:
                # The one case worth shouting about: inside the floor with
                # nowhere to go. Carrying is then impossible and the position
                # must be closed by a human before it expires.
                if "CLOSE, do not carry" in decision.reason:
                    send_alert_dedup(
                        f"roll_impossible:{self.bucket.id}:{pos.symbol}",
                        f"[{self.bucket.id}] {pos.symbol} is inside its expiry "
                        f"floor and has NO later contract to roll into. "
                        f"{decision.reason}",
                    )
                continue

            _log.info(
                "rolling_contract",
                bucket_id=self.bucket.id,
                symbol=pos.symbol,
                to=decision.to_contract.symbol if decision.to_contract else None,
                reason=decision.reason,
            )
            if not self._close_position(
                om, pos.strategy_name or "", pos, regime=None
            ):
                _log.error(
                    "roll_close_failed_not_reopening",
                    bucket_id=self.bucket.id,
                    symbol=pos.symbol,
                )
                continue
            rolled += 1

            if killed:
                _log.warning(
                    "roll_reopen_skipped_kill_switch",
                    bucket_id=self.bucket.id,
                    symbol=pos.symbol,
                )
                continue
            # The far leg is a fresh ENTRY in every respect the safety layers
            # care about, so it goes through the normal entry path on the next
            # tick rather than being force-placed here: it must be sized by the
            # allocator against current margin, carry its own protective stop,
            # and be refused if the preflight cannot price it. Placing it
            # inline would bypass all three.
            _log.info(
                "roll_reopen_deferred_to_entry_path",
                bucket_id=self.bucket.id,
                to=decision.to_contract.symbol if decision.to_contract else None,
            )
        return rolled

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
        # Decision 033: the mark at the moment we decided to exit. Exits carry
        # no signal_price — select_exits returns bare symbols, so there is no
        # per-symbol reference bar to read a close off without changing that
        # contract for every strategy. This still yields the EXECUTION half of
        # the exit's slippage, which is the actionable half.
        exit_mark = self._collect_mark_prices([pos.symbol]).get(pos.symbol)
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
                extra_payload=(
                    {"decision_price": str(exit_mark)} if exit_mark else None
                ),
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

    def _collect_mark_prices(
        self, symbols: list[str], failed: set[str] | None = None
    ) -> dict[str, Decimal]:
        """Mark price per symbol. Symbols whose FETCH raised land in ``failed``.

        The distinction matters downstream: an absent price and a failed call
        both leave the symbol out of the dict, but only the second is an
        infrastructure fault that cost a trade. See the sizer's missing-price
        branch.
        """
        out: dict[str, Decimal] = {}
        for s in symbols:
            try:
                t = self._data.get_ticker(s)
            except Exception:
                _log.warning("mark_price_fetch_failed", symbol=s, exc_info=True)
                if failed is not None:
                    failed.add(s)
                continue
            price = t.mark_price or t.last_price
            if price and price > 0:
                out[s] = price
        return out

    def _resolve_contracts(
        self,
        *,
        scanner: str,
        symbols: list[str],
        sides: dict[str, str],
        spot_prices: dict[str, Decimal],
    ) -> ExecutionPlan:
        """Map each candidate underlying onto the contract it will trade.

        Pass-through when the bucket declares no contract selection — the
        overwhelming majority of buckets — so this costs one dict lookup on the
        cash path and nothing else.

        A candidate is DROPPED when no contract qualifies (no expiry in the DTE
        window, no strike on the ladder) or when its premium cannot be fetched.
        Dropping rather than passing it on with a missing price is deliberate:
        the sizer's vocabulary for a missing price is "mark price unavailable",
        which would file an instrument-availability fact under an execution
        failure and mislead whoever reads it back.
        """
        config = self.contract_configs.get(scanner)
        if config is None:
            # No selection configured: the scanned symbol IS the traded symbol.
            return ExecutionPlan(
                symbols=symbols,
                exec_symbols={},
                exec_prices=spot_prices,
                lot_sizes={},
                multipliers={},
                contract_hints={},
            )

        registry = getattr(self._data, "fno", None)
        if registry is None:
            _log.error(
                "contract_selection_no_registry",
                bucket_id=self.bucket.id,
                scanner=scanner,
            )
            return ExecutionPlan([], {}, {}, {}, {}, {})

        selector = ContractSelector(registry, config)
        today = self._clock.now().date()
        kept: list[str] = []
        exec_symbols: dict[str, str] = {}
        exec_prices: dict[str, Decimal] = {}
        lot_sizes: dict[str, Decimal] = {}
        multipliers: dict[str, Decimal] = {}
        hints: dict[str, dict[str, object]] = {}

        for sym in symbols:
            spot = spot_prices.get(sym)
            if spot is None or spot <= 0:
                _log.warning(
                    "contract_selection_no_spot",
                    bucket_id=self.bucket.id,
                    underlying=sym,
                )
                continue
            chosen: Selection = selector.select(
                sym, spot=spot, side=sides.get(sym, "buy"), on=today
            )
            if not chosen.ok or chosen.contract is None:
                _log.warning(
                    "contract_selection_miss",
                    bucket_id=self.bucket.id,
                    underlying=sym,
                    reason=chosen.reason,
                )
                continue
            contract = chosen.contract
            premium = self._contract_price(contract.symbol)
            if premium is None or premium <= 0:
                _log.warning(
                    "contract_price_unavailable",
                    bucket_id=self.bucket.id,
                    underlying=sym,
                    contract=contract.symbol,
                )
                continue
            kept.append(sym)
            exec_symbols[sym] = contract.symbol
            exec_prices[sym] = premium
            lot_sizes[sym] = Decimal(contract.lot_size)
            multipliers[sym] = Decimal(
                getattr(contract, "multiplier", 0) or contract.lot_size
            )
            hints[sym] = contract_hint(contract)

        return ExecutionPlan(
            kept, exec_symbols, exec_prices, lot_sizes, multipliers, hints
        )

    def _contract_price(self, symbol: str) -> Decimal | None:
        """Last traded price of one contract, or None.

        Its own method so a venue hiccup on ONE contract drops ONE candidate
        rather than the whole strategy's tick.
        """
        try:
            ticker = self._data.get_ticker(symbol)
        except Exception:
            _log.warning(
                "contract_ticker_failed",
                bucket_id=self.bucket.id,
                symbol=symbol,
                exc_info=True,
            )
            return None
        price = getattr(ticker, "last_price", None)
        return price if price is not None and price > 0 else None

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

        EQUITY ONLY. This arithmetic assumes ``size`` is a share count and
        ``price`` is INR per share, matching ``margin_budget``. On crypto the
        units do not line up — ``size`` is contracts and ``price`` is USD per
        contract, with the fx and contract-size conversion already done by
        ``notional_inr_to_contracts`` — so an INR budget divided by a USD
        price yields a fraction, floors to 0, and would silently stop the
        bucket from trading at all. Crypto also has no need of this: it sets
        leverage explicitly per position (Decision 021), so the granted
        multiple is never in doubt.
        """
        if self.bucket.market != Market.INDIAN:
            return size
        if price is None or price <= 0 or size <= 0:
            return size
        needed = broker.required_margin(
            symbol, side, size, price, product=self.bucket.config.product
        )
        # Decision 036 — THERE IS NO 1x IN F&O.
        #
        # For cash equity the fallback below is sound: margin is a leverage
        # multiple of notional, so 1x is a quantity we can always afford. A
        # derivative's margin is SPAN + exposure, set by the exchange's risk
        # model against the UNDERLYING's notional — one NIFTY lot is ~Rs 15.8L
        # of exposure whose margin is ~Rs 1.9L, and no fraction of that is
        # "unleveraged". Sizing a derivative off a leverage guess would put an
        # order in that the venue prices at multiples of the budget, and on a
        # short option there is no bounded loss behind the mistake.
        #
        # So: no margin answer means no order. This is what makes the preflight
        # LOAD-BEARING rather than best-effort for these two buckets, and it is
        # the reason Phase C's gate is "required_margin answers correctly
        # against a live account" — a method never yet exercised on one.
        if needed is None and is_derivative(symbol):
            _log.error(
                "derivative_margin_preflight_unavailable_order_refused",
                bucket_id=self.bucket.id,
                symbol=symbol,
                wanted=str(size),
            )
            return Decimal("0")
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

    def _one_x_size(
        self,
        *,
        price: Decimal | None,
        margin_budget: Decimal,
        lot_size: Decimal = Decimal("1"),
    ) -> Decimal | None:
        """Largest whole quantity affordable with NO leverage, or None.

        This is the clamp for a leveraged→cash product fallback (Decision 029
        amended: an MIS-ineligible scrip trades 1x CNC instead of being
        skipped). At 1x, margin == notional, so the budget buys
        ``margin_budget / price`` shares — a quarter of what the same budget
        buys at 4x, which is exactly the point: the fallback must not spend
        more cash than the sizer allotted this slot.

        None when the bucket declares no ``fallback_product``, or the price is
        unusable — either way the adapter leaves the rejection to propagate.
        """
        if self.bucket.config.fallback_product is None:
            return None
        if price is None or price <= 0:
            return None
        # Decision 036 — a derivative has no cash equivalent to fall back to.
        # There is no CNC for a futures contract, so a "1x size" here would be
        # a quantity for a product that does not exist. The lot grid still
        # applies to whatever we do return.
        return quantize_to_lots(margin_budget / price, lot_size)

    def _place_order(
        self,
        *,
        broker: Broker,
        om: OrderManager,
        strat_name: str,
        symbol: str,
        side: str,
        size: Decimal,
        fallback_max_size: Decimal | None = None,
        extra_payload: dict[str, object] | None = None,
        mark_price: Decimal | None = None,
        stop_distance: Decimal | None = None,
    ) -> None:
        try:
            # Inside the try ON PURPOSE. If the venue can attach a stop but we
            # cannot compute one, this raises and the handler below skips the
            # ENTRY. That is the whole point of Decision 034: no protection, no
            # trade. Falling through to an unprotected entry would restore the
            # exact failure mode being removed.
            attached_stop, attached_target = self._attached_protection(
                broker=broker,
                symbol=symbol,
                side=side,
                mark_price=mark_price,
                stop_distance=stop_distance,
            )
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
                fallback_max_size=fallback_max_size,
                extra_payload=extra_payload,
                attached_stop_price=attached_stop,
                attached_target_price=attached_target,
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

    def _attached_protection(
        self,
        *,
        broker: Broker,
        symbol: str,
        side: str,
        mark_price: Decimal | None,
        stop_distance: Decimal | None,
    ) -> tuple[Decimal | None, Decimal | None]:
        """``(stop, target)`` to carry ON this entry order, or ``(None, None)``.

        ``(None, None)`` means "this venue protects positions the Decision 022
        way" — a separate stop rested by the sweep after the fill. Crypto takes
        that path unchanged.

        Where the venue CAN attach a stop, failing to produce one raises rather
        than returning None, because the caller treats an exception as "skip the
        entry" and None as "the old path will handle it". Confusing the two is
        how this feature would silently become a no-op.

        Note the reference price is the MARK at decision time, not the fill: a
        market entry has no fill price yet, and the stop has to be in the same
        request. That is closer to the backtest than the sweep's behaviour, not
        further — the backtest fixes the stop at the entry BAR — but it does
        mean slippage between mark and fill shifts the stop by that much.
        """
        if not broker.supports_attached_stop():
            return None, None
        # BOTH gates. The bucket flag is the rollout unit (one bucket at a
        # time); the setting is the process-wide master kill, so the feature can
        # be switched off on the VM without editing and redeploying YAML.
        if not (
            self.bucket.config.attached_stops
            and get_settings().attached_stops_enabled
        ):
            return None, None
        pct = self.bucket.config.stop_loss_pct
        if pct is None:
            # A bucket with no crash net configured has nothing to attach. The
            # sweep already pages about this via ``unprotectable``; do not also
            # block its entries.
            return None, None
        if mark_price is None or mark_price <= 0:
            raise ValueError(
                f"no mark price for {symbol}: cannot compute an attached stop, "
                "and an entry without one is exactly what Decision 034 forbids"
            )

        position_side = "long" if side == "buy" else "short"
        tick = broker.tick_size(symbol)
        band = self._price_band_pct(symbol)
        stop = resolve_stop_trigger(
            entry_price=mark_price,
            position_side=position_side,
            stop_pct=pct,
            distance=stop_distance,
            band_pct=band,
            tick=tick,
            symbol=symbol,
        )
        target = resolve_target_price(
            entry_price=mark_price,
            position_side=position_side,
            band_pct=band,
            tick=tick,
        )
        if stop <= 0 or target <= 0:
            raise ValueError(
                f"nonsensical attached protection for {symbol}: "
                f"stop={stop} target={target}"
            )
        return stop, target

    def _price_band_pct(self, symbol: str) -> Decimal | None:
        """The scrip's daily circuit band, from Dhan's scrip master.

        None when unknown — both callers degrade sensibly (the stop keeps its
        configured distance, the target falls back to a tight default).
        """
        try:
            universe = self._data.universe  # type: ignore[attr-defined]
        except Exception:
            return None
        raw = ((universe or {}).get(symbol) or {}).get("band_pct")
        if not raw:
            return None
        try:
            value = Decimal(str(raw))
        except (ArithmeticError, ValueError):
            return None
        return value if value > 0 else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hint_decimal(hint: dict[str, object], key: str) -> Decimal | None:
    """One numeric field off a strategy hint, or None if absent/unparseable.

    Strategy hints are free-form dicts, so an unusable value must degrade to
    None rather than raise — the caller then falls back to the bucket's percent
    net, which is the pre-Decision-032 behaviour.
    """
    raw = hint.get(key)
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None
    return value if value > 0 else None


def _entry_extra(
    *,
    hint: dict[str, object],
    margin_inr: Decimal,
    decision_price: Decimal | None = None,
    contract: dict[str, object] | None = None,
    underlying_price: Decimal | None = None,
) -> dict[str, object]:
    """Facts stamped on the entry Trade for downstream stages to read back.

    ``stop_distance`` (from the strategy's ``EntryCandidate.hint``) is what the
    stop sweep rests the broker-side protective order at — Decision 032's
    per-instrument ATR stop, instead of the bucket-wide percent net.
    ``margin_inr`` is the own-capital the sizer allotted this slot, which is
    what the MTF carry-interest charge measures the funded portion against.

    ``signal_price`` (the close of the bar the strategy decided on) and
    ``decision_price`` (the mark when we actually placed the order) are the two
    reference points slippage is measured from — Decision 033. Recording them
    at decision time is the only chance we get: neither is recoverable
    afterwards, because "what the strategy saw" is not a thing the exchange
    knows. With ``avg_fill_price`` from the reconciler they separate latency
    cost from execution cost; see ``src/reporting/slippage.py``.

    All values are plain strings so the JSONB round-trips losslessly.
    """
    out: dict[str, object] = {"margin_inr": str(margin_inr)}
    distance = hint.get("stop_distance")
    if distance is not None:
        out["stop_distance"] = str(distance)
    signal = hint.get("signal")
    if signal is not None:
        out["signal"] = str(signal)
    signal_price = hint.get("signal_price")
    if signal_price is not None:
        out["signal_price"] = str(signal_price)
    if decision_price is not None:
        out["decision_price"] = str(decision_price)
    # Decision 036 — which contract this signal resolved to, and the spot that
    # chose it. Both are unrecoverable afterwards: the exchange knows neither
    # the rule nor the spot we read, so without them a fill on
    # NIFTY-20260908-23150-CE cannot be traced back to the decision that
    # produced it, and "why that strike?" has no answer.
    if contract:
        out.update(contract)
    if underlying_price is not None:
        out["underlying_price"] = str(underlying_price)
    return out


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
