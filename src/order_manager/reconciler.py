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

            # Case 2: Exchange has position, DB doesn't → orphan
            for sym, ex_pos in exchange_by_symbol.items():
                if sym not in db_symbols:
                    diff = {
                        "type": "orphan_position",
                        "symbol": sym,
                        "exchange_side": ex_pos.side,
                        "exchange_size": str(ex_pos.size),
                    }
                    report.diffs.append(diff)
                    report.orphan_positions += 1
                    session.add(
                        AuditLog(
                            event_type=AuditEventType.RECONCILE_DIFF,
                            message=f"Orphan position {sym} found on exchange, not in DB",
                            payload=diff,
                        )
                    )
                    self._log.warning("orphan_position", **diff)

            # Case 3: Both have position → verify size matches
            for db_pos in db_positions:
                ex_pos = exchange_by_symbol.get(db_pos.symbol)
                if ex_pos is None:
                    continue
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
