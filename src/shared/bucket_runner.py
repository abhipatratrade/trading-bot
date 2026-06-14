"""
BucketRunner — the per-bucket pipeline orchestrator.

Implements the eight-step pipeline from the plan / PPTX slide 3:

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

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from src.brokers.base import Broker, OrderType
from src.core.alerts import send_alert
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    MarketRegime,
    SizingDecision,
)
from src.data_sources.base import MarketData
from src.order_manager.manager import KillSwitchEngagedError, OrderManager
from src.safety import kill_switch
from src.shared.allocator.sizer import (
    AllocatorConfig,
    load_allocator_config,
    size_positions,
)
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
from src.shared.base_strategy import Strategy

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


class BucketRunner:
    """One pipeline per bucket. Configs loaded eagerly on construction."""

    def __init__(
        self,
        *,
        bucket: Bucket,
        brokers: Mapping[BrokerName, Broker],
        data: MarketData,
        order_managers: Mapping[BrokerName, OrderManager],
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

        broker = self._brokers.get(self.bucket.config.broker)
        order_manager = self._oms.get(self.bucket.config.broker)
        if broker is None or order_manager is None:
            _log.warning(
                "bucket_broker_unavailable",
                bucket_id=self.bucket.id,
                wanted=self.bucket.config.broker.value,
            )
            return _empty_summary(self.bucket.id)

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
        )

    # ── Internals ──────────────────────────────────────────────────────
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
        size: Decimal,
    ) -> None:
        try:
            om.place_order(
                strategy_id=self.bucket.id,
                bucket_id=self.bucket.id,
                strategy_name=strat_name,
                symbol=symbol,
                side="buy",
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
            send_alert(
                f"[{self.bucket.id}] FAILED to open {symbol} via {strat_name}"
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
