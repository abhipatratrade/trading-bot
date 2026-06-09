"""
Idempotent order placement.

Every order gets a deterministic ``client_order_id`` derived from
(strategy, symbol, side, minute).  Retries within the same minute
return the existing Trade row instead of double-firing.

The kill switch is checked before any order is submitted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.brokers.base import (
    Broker,
    OrderRequest,
    OrderType,
    TimeInForce,
)
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    KillSwitch,
    KillSwitchScope,
    OrderSide,
    OrderStatus,
    Trade,
)


class KillSwitchEngagedError(Exception):
    """Raised when the kill switch prevents an order."""


@dataclass(frozen=True, slots=True)
class PlacementResult:
    """Returned by :meth:`OrderManager.place_order`."""

    trade_id: int
    client_order_id: str
    exchange_order_id: str | None
    status: OrderStatus
    was_existing: bool
    raw: dict[str, Any]


def make_client_order_id(
    strategy_id: str,
    symbol: str,
    side: str,
    intent_ts: datetime,
    intent_id: str = "",
) -> str:
    """Deterministic 32-char hex ID.  Same inputs → same ID → idempotent."""
    minute = intent_ts.strftime("%Y%m%d%H%M")
    raw = f"{strategy_id}:{symbol}:{side}:{minute}:{intent_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class OrderManager:
    """Coordinates order placement between the broker and the database.

    Usage::

        mgr = OrderManager(broker, BrokerName.DELTA_INDIA)
        result = mgr.place_order("crypto_longterm", "BTCUSD", "buy", ...)
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
        self._log = get_logger("order_manager")

    # ── Public API ──────────────────────────────────────────────────

    def place_order(
        self,
        strategy_id: str,
        symbol: str,
        side: str,
        size: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: Decimal | None = None,
        leverage: Decimal | None = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
        reduce_only: bool = False,
        intent_id: str = "",
        bucket_id: str | None = None,
        strategy_name: str | None = None,
    ) -> PlacementResult:
        now = self._clock.now()
        client_oid = make_client_order_id(
            strategy_id, symbol, side, now, intent_id
        )

        # 1. Kill switch
        with session_scope() as session:
            self._check_kill_switch(session, strategy_id)

        # 2. Idempotency: return existing if already placed
        with session_scope() as session:
            existing = session.execute(
                select(Trade).where(Trade.client_order_id == client_oid)
            ).scalar_one_or_none()
            if existing and existing.status in (
                OrderStatus.PENDING,
                OrderStatus.OPEN,
                OrderStatus.FILLED,
                OrderStatus.PARTIAL,
            ):
                self._log.info(
                    "order_idempotent_hit",
                    client_order_id=client_oid,
                    status=existing.status.value,
                )
                return PlacementResult(
                    trade_id=existing.id,
                    client_order_id=client_oid,
                    exchange_order_id=existing.exchange_order_id,
                    status=existing.status,
                    was_existing=True,
                    raw={},
                )

        # 3. Persist PENDING trade
        trade = Trade(
            strategy_id=strategy_id,
            bucket_id=bucket_id,
            strategy_name=strategy_name,
            broker=self._broker_name,
            symbol=symbol,
            side=OrderSide(side),
            quantity=size,
            price=limit_price,
            leverage=leverage,
            client_order_id=client_oid,
            status=OrderStatus.PENDING,
            submitted_at=now,
        )
        with session_scope() as session:
            session.add(trade)
            session.flush()
            trade_id = trade.id

        # 4. Set leverage if requested
        if leverage is not None:
            try:
                self._broker.set_leverage(symbol, leverage)
            except Exception:
                self._log.warning(
                    "set_leverage_failed", symbol=symbol, leverage=str(leverage)
                )

        # 5. Send to broker
        try:
            result = self._broker.place_order(
                OrderRequest(
                    symbol=symbol,
                    side=side,
                    size=size,
                    order_type=order_type,
                    limit_price=limit_price,
                    time_in_force=time_in_force,
                    reduce_only=reduce_only,
                    client_order_id=client_oid,
                )
            )
        except Exception:
            with session_scope() as session:
                t = session.get(Trade, trade_id)
                if t:
                    t.status = OrderStatus.REJECTED
                session.add(
                    AuditLog(
                        strategy_id=strategy_id,
                        event_type=AuditEventType.ORDER_PLACED,
                        message=f"REJECTED {side} {size} {symbol}",
                        payload={"client_order_id": client_oid, "reason": "broker_error"},
                    )
                )
            raise

        # 6. Update trade with exchange response
        mapped_status = _map_broker_status(result.status)
        with session_scope() as session:
            t = session.get(Trade, trade_id)
            if t:
                t.exchange_order_id = result.exchange_order_id
                t.status = mapped_status
            session.add(
                AuditLog(
                    strategy_id=strategy_id,
                    event_type=AuditEventType.ORDER_PLACED,
                    message=f"{side} {size} {symbol} @ {limit_price or 'market'}",
                    payload={
                        "client_order_id": client_oid,
                        "exchange_order_id": result.exchange_order_id,
                        "status": mapped_status.value,
                    },
                )
            )

        self._log.info(
            "order_placed",
            trade_id=trade_id,
            exchange_order_id=result.exchange_order_id,
            status=mapped_status.value,
        )

        return PlacementResult(
            trade_id=trade_id,
            client_order_id=client_oid,
            exchange_order_id=result.exchange_order_id,
            status=mapped_status,
            was_existing=False,
            raw=result.raw,
        )

    def cancel_order(
        self,
        strategy_id: str,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> bool:
        """Cancel an order.  Returns True on success."""
        result = self._broker.cancel_order(
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
            symbol=symbol,
        )

        # Update DB trade
        with session_scope() as session:
            trade: Trade | None = None
            if exchange_order_id:
                trade = session.execute(
                    select(Trade).where(
                        Trade.exchange_order_id == exchange_order_id
                    )
                ).scalar_one_or_none()
            elif client_order_id:
                trade = session.execute(
                    select(Trade).where(
                        Trade.client_order_id == client_order_id
                    )
                ).scalar_one_or_none()
            if trade:
                trade.status = OrderStatus.CANCELED
            session.add(
                AuditLog(
                    strategy_id=strategy_id,
                    event_type=AuditEventType.ORDER_CANCELED,
                    message=f"Canceled {symbol} order {exchange_order_id or client_order_id}",
                    payload={
                        "exchange_order_id": result.exchange_order_id,
                        "symbol": symbol,
                    },
                )
            )

        self._log.info(
            "order_canceled",
            exchange_order_id=result.exchange_order_id,
            symbol=symbol,
        )
        return result.success

    # ── Internal ────────────────────────────────────────────────────

    @staticmethod
    def _check_kill_switch(session: Any, strategy_id: str) -> None:
        global_kill = session.execute(
            select(KillSwitch).where(
                KillSwitch.scope == KillSwitchScope.GLOBAL,
                KillSwitch.engaged.is_(True),
            )
        ).scalar_one_or_none()
        if global_kill:
            raise KillSwitchEngagedError(
                f"Global kill switch engaged: {global_kill.reason}"
            )
        strat_kill = session.execute(
            select(KillSwitch).where(
                KillSwitch.scope == KillSwitchScope.STRATEGY,
                KillSwitch.strategy_id == strategy_id,
                KillSwitch.engaged.is_(True),
            )
        ).scalar_one_or_none()
        if strat_kill:
            raise KillSwitchEngagedError(
                f"Kill switch engaged for {strategy_id}: {strat_kill.reason}"
            )


def _map_broker_status(status_str: str) -> OrderStatus:
    return {
        "open": OrderStatus.OPEN,
        "pending": OrderStatus.PENDING,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
    }.get(status_str, OrderStatus.UNKNOWN)
