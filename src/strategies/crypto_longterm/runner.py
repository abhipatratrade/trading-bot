"""
Crypto long-term strategy runner — daily rebalance loop.

Flow per rebalance tick:
  1. Check kill switch.
  2. Run volume scanner → today's universe (top 5 by Delta 24h volume).
  3. Run safety breakers.
  4. Diff current positions vs new universe.
  5. Close positions not in new universe (reduce-only sells).
  6. Open new positions at equal weight × leverage.
  7. Audit-log every decision.

The runner is called once per day at the configured rebalance hour.
Between rebalances, it just checks safety and sleeps.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select

from src.brokers.base import Broker, OrderType
from src.core.alerts import send_alert
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    Position,
    PositionSide,
)
from src.data_sources.base import MarketData
from src.order_manager.manager import KillSwitchEngagedError, OrderManager
from src.safety import breakers, kill_switch
from src.scanner.volume_scanner import run_volume_scan
from src.strategies.crypto_longterm.schema import CryptoLongtermPolicy

_log = get_logger("strategies.crypto_longterm.runner")


class CryptoLongtermRunner:
    """Orchestrates the crypto long-term strategy."""

    def __init__(
        self,
        policy: CryptoLongtermPolicy,
        broker: Broker,
        data_source: MarketData,
        order_manager: OrderManager,
        clock: Clock | None = None,
    ) -> None:
        self._policy = policy
        self._broker = broker
        self._data = data_source
        self._om = order_manager
        self._clock = clock or RealClock()
        self._last_rebalance_date: date | None = None

    @property
    def strategy_id(self) -> str:
        return self._policy.strategy_id

    # ── Main loop tick ─────────────────────────────────────────────

    def tick(self) -> None:
        """Called every loop iteration (e.g. every 60s).

        Decides whether to run a full rebalance or just a safety check.
        """
        if kill_switch.is_engaged(self.strategy_id):
            _log.info("tick_skipped_kill_switch", strategy_id=self.strategy_id)
            return

        now = self._clock.now()
        today = now.date()

        should_rebalance = (
            self._last_rebalance_date != today
            and now.hour >= self._policy.rebalance_hour
            and now.minute >= self._policy.rebalance_minute
        )

        if should_rebalance:
            self._run_rebalance(today, now)
            self._last_rebalance_date = today
        else:
            self._run_safety_check()

    # ── Rebalance ──────────────────────────────────────────────────

    def _run_rebalance(self, today: date, now: datetime) -> None:
        _log.info("rebalance_start", date=str(today))

        # 1. Run scanner
        scan = run_volume_scan(
            strategy_id=self.strategy_id,
            data_source=self._data,
            scan_date=today,
            max_positions=self._policy.max_positions,
            min_24h_volume_usd=self._policy.min_24h_volume_usd,
        )
        new_universe = set(scan.universe)

        if not new_universe:
            _log.warning("empty_universe", date=str(today))
            send_alert(f"[{self.strategy_id}] Scanner returned empty universe on {today}")
            return

        # 2. Safety check before trading
        if self._run_safety_check():
            _log.warning("rebalance_aborted_breaker", date=str(today))
            return

        # 3. Get current positions from DB
        current_positions = self._get_db_positions()
        current_symbols = set(current_positions.keys())

        # 4. Determine exits and entries
        to_close = current_symbols - new_universe
        to_open = new_universe - current_symbols
        to_keep = current_symbols & new_universe

        _log.info(
            "rebalance_diff",
            to_close=list(to_close),
            to_open=list(to_open),
            to_keep=list(to_keep),
        )

        with session_scope() as session:
            session.add(
                AuditLog(
                    strategy_id=self.strategy_id,
                    event_type=AuditEventType.UNIVERSE_CHANGE,
                    message=f"Rebalance: close={list(to_close)}, open={list(to_open)}, keep={list(to_keep)}",
                    payload={
                        "date": str(today),
                        "new_universe": list(new_universe),
                        "current_symbols": list(current_symbols),
                        "to_close": list(to_close),
                        "to_open": list(to_open),
                        "to_keep": list(to_keep),
                    },
                )
            )

        # 5. Close exiting positions
        for sym in to_close:
            self._close_position(sym, current_positions[sym], now)

        # 6. Calculate position sizes for new entries
        if to_open:
            sizes = self._calculate_sizes(to_open, new_universe, today)
            for sym in to_open:
                size = sizes.get(sym)
                if size and size > 0:
                    self._open_position(sym, size, now)

        send_alert(
            f"[{self.strategy_id}] Rebalance done: "
            f"closed={list(to_close)}, opened={list(to_open)}, "
            f"kept={list(to_keep)}"
        )
        _log.info("rebalance_complete", date=str(today))

    # ── Position management ────────────────────────────────────────

    def _close_position(self, symbol: str, pos: Position, now: datetime) -> None:
        """Close a position by placing a reduce-only market order."""
        side = "sell" if pos.side == PositionSide.LONG else "buy"
        _log.info("closing_position", symbol=symbol, side=side, quantity=str(pos.quantity))

        try:
            self._om.place_order(
                strategy_id=self.strategy_id,
                symbol=symbol,
                side=side,
                size=pos.quantity,
                order_type=OrderType.MARKET,
                reduce_only=True,
                intent_id=f"close-{now.strftime('%Y%m%d')}",
            )
        except KillSwitchEngagedError:
            _log.warning("close_blocked_kill_switch", symbol=symbol)
            raise
        except Exception:
            _log.error("close_position_failed", symbol=symbol, exc_info=True)
            send_alert(f"[{self.strategy_id}] FAILED to close {symbol}")

    def _open_position(self, symbol: str, size: Decimal, now: datetime) -> None:
        """Open a long position via market order."""
        _log.info("opening_position", symbol=symbol, size=str(size))

        try:
            self._om.place_order(
                strategy_id=self.strategy_id,
                symbol=symbol,
                side="buy",
                size=size,
                order_type=OrderType.MARKET,
                leverage=Decimal(str(self._policy.leverage)),
                intent_id=f"open-{now.strftime('%Y%m%d')}",
            )
        except KillSwitchEngagedError:
            _log.warning("open_blocked_kill_switch", symbol=symbol)
            raise
        except Exception:
            _log.error("open_position_failed", symbol=symbol, exc_info=True)
            send_alert(f"[{self.strategy_id}] FAILED to open {symbol}")

    def _calculate_sizes(
        self,
        to_open: set[str],
        full_universe: set[str],
        today: date,
    ) -> dict[str, Decimal]:
        """Calculate contract sizes for new entries.

        equal-weight: total_capital / N positions, then × leverage / mark_price.
        Uses Delta India mark_price for sizing.
        """
        balances = self._broker.get_balances()
        total_equity = sum(
            (b.available + b.order_margin + b.position_margin for b in balances),
            Decimal("0"),
        )

        if total_equity <= 0:
            _log.warning("no_equity_for_sizing")
            return {}

        n_total = len(full_universe)
        weight = Decimal("1") / Decimal(str(n_total))
        capital_per_slot = total_equity * weight
        notional_per_slot = capital_per_slot * Decimal(str(self._policy.leverage))

        sizes: dict[str, Decimal] = {}
        for sym in to_open:
            try:
                ticker = self._data.get_ticker(sym)
            except Exception:
                _log.error("ticker_fetch_failed_for_sizing", symbol=sym, exc_info=True)
                continue

            price = ticker.mark_price or ticker.last_price
            if not price or price <= 0:
                _log.warning("invalid_price_for_sizing", symbol=sym)
                continue

            # Delta India uses integer contract sizes
            contracts = (notional_per_slot / price).quantize(Decimal("1"), rounding=ROUND_DOWN)
            if contracts >= 1:
                sizes[sym] = contracts

        _log.info(
            "position_sizes_calculated",
            total_equity=str(total_equity),
            n_total=n_total,
            notional_per_slot=str(notional_per_slot),
            sizes={k: str(v) for k, v in sizes.items()},
        )
        return sizes

    # ── Safety ─────────────────────────────────────────────────────

    def _run_safety_check(self) -> bool:
        """Run breakers. Returns True if any tripped (and engages kill switch)."""
        held_symbols = list(self._get_db_positions().keys())

        results = breakers.run_all_breakers(
            self._broker,
            self._data,
            held_symbols,
            max_drawdown_pct=self._policy.max_daily_drawdown_pct,
            min_liq_distance_pct=self._policy.min_liquidation_distance_pct,
            max_funding_rate=self._policy.max_funding_rate,
        )

        tripped = [r for r in results if r.tripped]
        if tripped:
            names = [r.name for r in tripped]
            reason = f"Breaker(s) tripped: {', '.join(names)}"
            kill_switch.engage(reason, strategy_id=self.strategy_id)
            send_alert(f"[{self.strategy_id}] KILL SWITCH: {reason}")

            with session_scope() as session:
                for r in tripped:
                    session.add(
                        AuditLog(
                            strategy_id=self.strategy_id,
                            event_type=AuditEventType.BREAKER_TRIPPED,
                            message=f"Breaker {r.name} tripped",
                            payload=r.detail,
                        )
                    )
            return True

        return False

    # ── DB helpers ─────────────────────────────────────────────────

    def _get_db_positions(self) -> dict[str, Position]:
        """Return current open positions for this strategy, keyed by symbol."""
        with session_scope() as session:
            rows = session.execute(
                select(Position).where(
                    Position.strategy_id == self.strategy_id,
                    Position.broker == BrokerName.DELTA_INDIA,
                    Position.side != PositionSide.FLAT,
                    Position.quantity > 0,
                )
            ).scalars().all()
            # Detach from session so they're usable outside
            result = {}
            for r in rows:
                session.expunge(r)
                result[r.symbol] = r
            return result
