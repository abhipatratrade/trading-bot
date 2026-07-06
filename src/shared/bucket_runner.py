"""
BucketRunner — the per-bucket pipeline orchestrator.

Implements the eight-step pipeline from the plan / PPTX slide 3, plus the
exit step added by Decision 021:

    0. Strategy exits (select_exits per strategy — runs even for strategies
       currently blocked by the master/regime gate, since a gated strategy
       must still manage positions it already holds)
    1. Strategy discovery (folder scan)
    2. Strategy Master gate
    3. Market Scanner
    4. Regime Selector (Brain)
    5. Regime gate per strategy
    6. Dedup gate (Sizer)
    7. Position Size Allocator (Kelly + bucket capital)
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
    load_allocator_config,
    size_positions,
)
from src.shared.base_strategy import Strategy
from src.shared.bucket import Bucket
from src.shared.regime.brain import RegimeConfig, load_regime_config, predict_regime
from src.shared.regime.store import MARKET_SENTINEL
from src.shared.scanner.engine import (
    ScannerConfig,
    load_scanner_config,
    run_scan,
)
from src.shared.strategy_loader import discover_strategies
from src.shared.strategy_master.loader import (
    StrategyMaster,
    load_strategy_master,
)

_log = get_logger("shared.bucket_runner")


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
        self.scanner_config: ScannerConfig = load_scanner_config(
            bucket.scanner_yaml_path
        )
        self.regime_config: RegimeConfig = load_regime_config(
            bucket.regime_yaml_path
        )
        self.allocator_config: AllocatorConfig = load_allocator_config(
            bucket.allocator_yaml_path
        )
        self.strategies: dict[str, type[Strategy]] = discover_strategies(
            bucket.strategies_folder
        )

    # ── Main entry ─────────────────────────────────────────────────────
    def run_once(self) -> RunSummary:
        """Execute one full pipeline pass for this bucket."""
        _log.info("bucket_run_start", bucket_id=self.bucket.id)

        if not self.bucket.config.enabled:
            _log.info("bucket_disabled_skip", bucket_id=self.bucket.id)
            return _empty_summary(self.bucket.id)

        if kill_switch.is_engaged(self.bucket.id):
            _log.info("bucket_kill_switch_skip", bucket_id=self.bucket.id)
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

        # 0. Exits — strategy-driven closes run before any entry logic
        # (Decision 021). A failed exit must not block other strategies.
        exited = self._run_exits(order_manager)

        # 1+2: discovery + master gate (already loaded; gate is per-strategy below).

        # 3. Scanner
        scan = run_scan(
            bucket_id=self.bucket.id,
            data=self._data,
            config=self.scanner_config,
            scan_date=self._clock.now().date(),
            require_binance_listed=(
                self.bucket.market.value == "crypto"
            ),
        )

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
            entry_candidates = strategy.select_entries(scan.universe, self._data)
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

            results = size_positions(
                bucket=self.bucket,
                strategy_name=strat_name,
                candidates=symbols,
                mark_prices_inr=mark_prices,
                regimes=regimes,
                config=self.allocator_config,
            )

            for sym, res in results.items():
                if res.decision == SizingDecision.PLACED:
                    self._place_order(
                        broker=broker,
                        om=order_manager,
                        strat_name=strat_name,
                        symbol=sym,
                        side=sides.get(sym, "buy"),
                        size=res.contracts,
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
            universe=scan.universe,
            regime=market_regime,
            exited=exited,
        )

    # ── Internals ──────────────────────────────────────────────────────
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
            if t.extra and t.extra.get("reduce_only")
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

    def _place_order(
        self,
        *,
        broker: Broker,  # noqa: ARG002 — passed for parity / future hooks
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
