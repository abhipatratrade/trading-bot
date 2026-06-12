"""
DB ↔ exchange reconciler.

Runs at startup and every 5 minutes to catch discrepancies between
what the database thinks the exchange state is and what the exchange
actually reports.  Every diff is logged to the ``audit_log`` table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.brokers.base import Broker
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    OrderStatus,
    Position,
    PositionSide,
    Trade,
)


@dataclass
class ReconcileReport:
    """Summary of what the reconciler found and fixed."""

    positions_updated: int = 0
    positions_closed: int = 0
    orphan_positions: int = 0
    orders_updated: int = 0
    diffs: list[dict[str, Any]] = field(default_factory=list)


class Reconciler:
    """Compares DB state against the live exchange and fixes discrepancies.

    Usage::

        rec = Reconciler(broker, BrokerName.DELTA_INDIA)
        report = rec.run()
    """

    def __init__(
        self,
        broker: Broker,
        broker_name: BrokerName,
        clock: Clock | None = None,
    ) -> None:
        self._broker = broker
        self._broker_name = broker_name
        self._clock = clock or RealClock()
        self._log = get_logger("order_manager.reconciler")

    def run(self) -> ReconcileReport:
        """Full reconciliation pass: positions then orders."""
        report = ReconcileReport()
        self._reconcile_positions(report)
        self._reconcile_orders(report)

        if report.diffs:
            self._log.warning(
                "reconcile_diffs_found",
                count=len(report.diffs),
                positions_updated=report.positions_updated,
                orders_updated=report.orders_updated,
            )
        else:
            self._log.info("reconcile_clean")
        return report

    # ── Position reconciliation ─────────────────────────────────────

    def _reconcile_positions(self, report: ReconcileReport) -> None:
        exchange_positions = self._broker.get_positions()
        exchange_by_symbol: dict[str, Any] = {
            p.symbol: p for p in exchange_positions
        }

        with session_scope() as session:
            # All non-flat DB positions for this broker
            db_positions = list(
                session.execute(
                    select(Position).where(
                        Position.broker == self._broker_name,
                        Position.side != PositionSide.FLAT,
                    )
                ).scalars()
            )
            db_symbols = {p.symbol for p in db_positions}

            # Case 1: DB has position, exchange doesn't → close it
            for db_pos in db_positions:
                if db_pos.symbol not in exchange_by_symbol:
                    diff = {
                        "type": "position_closed_on_exchange",
                        "symbol": db_pos.symbol,
                        "db_side": db_pos.side.value,
                        "db_size": str(db_pos.quantity),
                    }
                    report.diffs.append(diff)
                    report.positions_closed += 1
                    db_pos.side = PositionSide.FLAT
                    db_pos.quantity = Decimal("0")
                    db_pos.closed_at = self._clock.now()
                    session.add(
                        AuditLog(
                            strategy_id=db_pos.strategy_id,
                            event_type=AuditEventType.RECONCILE_DIFF,
                            message=f"Position {db_pos.symbol} closed on exchange but open in DB",
                            payload=diff,
                        )
                    )
                    self._log.warning("position_closed_on_exchange", **diff)

            # Case 2: Exchange has position, DB doesn't → IMPORT IT.
            # Without this, the bot's dedup gate in shared.allocator.sizer
            # never sees the position and keeps placing new orders every
            # tick. Bucket attribution comes from the most-recent filled
            # Trade for the symbol; truly external positions stay
            # unattributed and we just emit a warning.
            for sym, ex_pos in exchange_by_symbol.items():
                if sym in db_symbols:
                    continue
                latest_trade = self._latest_filled_trade(session, sym)
                strategy_id = latest_trade.strategy_id if latest_trade else "unknown"
                bucket_id = latest_trade.bucket_id if latest_trade else None
                strategy_name = (
                    latest_trade.strategy_name if latest_trade else None
                )
                side = _exchange_side_to_position(ex_pos.side)
                diff = {
                    "type": "orphan_position_imported",
                    "symbol": sym,
                    "exchange_side": ex_pos.side,
                    "exchange_size": str(ex_pos.size),
                    "bucket_id": bucket_id,
                    "strategy_name": strategy_name,
                    "strategy_id": strategy_id,
                    "source_trade_id": latest_trade.id if latest_trade else None,
                }
                report.diffs.append(diff)
                report.orphan_positions += 1
                session.add(
                    Position(
                        strategy_id=strategy_id,
                        bucket_id=bucket_id,
                        strategy_name=strategy_name,
                        broker=self._broker_name,
                        symbol=sym,
                        side=side,
                        quantity=ex_pos.size,
                        entry_price=ex_pos.entry_price,
                        leverage=ex_pos.leverage,
                        liquidation_price=ex_pos.liquidation_price,
                        opened_at=self._clock.now(),
                    )
                )
                session.add(
                    AuditLog(
                        strategy_id=strategy_id,
                        event_type=AuditEventType.RECONCILE_DIFF,
                        message=f"Orphan position {sym} imported from exchange",
                        payload=diff,
                    )
                )
                self._log.warning("orphan_position_imported", **diff)

            # Case 3: Both have position → verify size matches AND backfill
            # bucket_id / strategy_name if they were never set (rows created
            # under the legacy schema, or by an old code path).
            for db_pos in db_positions:
                ex_pos = exchange_by_symbol.get(db_pos.symbol)
                if ex_pos is None:
                    continue

                if db_pos.bucket_id is None or db_pos.strategy_name is None:
                    latest_trade = self._latest_filled_trade(
                        session, db_pos.symbol
                    )
                    if latest_trade is not None:
                        if db_pos.bucket_id is None and latest_trade.bucket_id:
                            db_pos.bucket_id = latest_trade.bucket_id
                        if (
                            db_pos.strategy_name is None
                            and latest_trade.strategy_name
                        ):
                            db_pos.strategy_name = latest_trade.strategy_name

                if abs(db_pos.quantity - ex_pos.size) > Decimal("0.0001"):
                    diff = {
                        "type": "position_size_mismatch",
                        "symbol": db_pos.symbol,
                        "db_size": str(db_pos.quantity),
                        "exchange_size": str(ex_pos.size),
                    }
                    report.diffs.append(diff)
                    report.positions_updated += 1
                    db_pos.quantity = ex_pos.size
                    db_pos.entry_price = ex_pos.entry_price
                    if ex_pos.liquidation_price:
                        db_pos.liquidation_price = ex_pos.liquidation_price
                    session.add(
                        AuditLog(
                            strategy_id=db_pos.strategy_id,
                            event_type=AuditEventType.RECONCILE_DIFF,
                            message=f"Position {db_pos.symbol} size mismatch, updated to exchange",
                            payload=diff,
                        )
                    )
                    self._log.warning("position_size_mismatch", **diff)

    def _latest_filled_trade(self, session, symbol: str) -> "Trade | None":
        """Most-recent FILLED Trade for this symbol on this broker.

        Used to attribute orphan positions back to the bucket / strategy
        that originally opened them.
        """
        return session.execute(
            select(Trade)
            .where(
                Trade.broker == self._broker_name,
                Trade.symbol == symbol,
                Trade.status == OrderStatus.FILLED,
            )
            .order_by(Trade.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    # ── Order reconciliation ────────────────────────────────────────

    def _reconcile_orders(self, report: ReconcileReport) -> None:
        exchange_open = self._broker.get_open_orders()
        open_ids = {o.exchange_order_id for o in exchange_open}

        with session_scope() as session:
            pending_trades = list(
                session.execute(
                    select(Trade).where(
                        Trade.broker == self._broker_name,
                        Trade.status.in_([
                            OrderStatus.PENDING,
                            OrderStatus.OPEN,
                        ]),
                    )
                ).scalars()
            )

            for trade in pending_trades:
                if trade.exchange_order_id and trade.exchange_order_id in open_ids:
                    if trade.status == OrderStatus.PENDING:
                        trade.status = OrderStatus.OPEN
                        report.orders_updated += 1
                    continue

                # Order is no longer open — check what happened
                new_status = OrderStatus.UNKNOWN
                if trade.exchange_order_id:
                    order_info = self._broker.get_order(trade.exchange_order_id)
                    if order_info:
                        new_status = _map_status(order_info.status)

                if new_status == trade.status:
                    continue

                diff = {
                    "type": "order_status_changed",
                    "exchange_order_id": trade.exchange_order_id,
                    "client_order_id": trade.client_order_id,
                    "symbol": trade.symbol,
                    "old_status": trade.status.value,
                    "new_status": new_status.value,
                }
                report.diffs.append(diff)
                report.orders_updated += 1
                trade.status = new_status
                if new_status == OrderStatus.FILLED:
                    trade.filled_at = self._clock.now()
                session.add(
                    AuditLog(
                        strategy_id=trade.strategy_id,
                        event_type=AuditEventType.RECONCILE_DIFF,
                        message=f"Order {trade.exchange_order_id} status changed",
                        payload=diff,
                    )
                )
                self._log.info("order_status_updated", **diff)


def _map_status(status_str: str) -> OrderStatus:
    return {
        "open": OrderStatus.OPEN,
        "pending": OrderStatus.PENDING,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
    }.get(status_str, OrderStatus.UNKNOWN)


def _exchange_side_to_position(side: str) -> PositionSide:
    """Map the broker's free-form side string to our enum.

    Delta India and most perp venues report ``"long"`` / ``"short"`` /
    ``"flat"``; some return ``"buy"`` / ``"sell"`` from the order side.
    Anything we can't classify becomes FLAT so we don't silently
    misattribute a position.
    """
    s = (side or "").lower()
    if s in ("long", "buy"):
        return PositionSide.LONG
    if s in ("short", "sell"):
        return PositionSide.SHORT
    return PositionSide.FLAT
