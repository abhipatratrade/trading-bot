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
    AttachedStopRetireError,
    Broker,
    OrderRequest,
    OrderType,
    TimeInForce,
)
from src.core.alerts import send_alert
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

    @property
    def broker_name(self) -> BrokerName:
        """The broker this manager places orders on."""
        return self._broker_name

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
        stop_price: Decimal | None = None,
        attached_stop_price: Decimal | None = None,
        attached_target_price: Decimal | None = None,
        product: str | None = None,
        fallback_max_size: Decimal | None = None,
        intent_id: str = "",
        bucket_id: str | None = None,
        strategy_name: str | None = None,
        allow_when_killed: bool = False,
        extra_payload: dict[str, Any] | None = None,
    ) -> PlacementResult:
        now = self._clock.now()
        client_oid = make_client_order_id(
            strategy_id, symbol, side, now, intent_id
        )

        # 0. An attached stop (Decision 034) is only safe on a venue that
        # actually honours it. An adapter that quietly ignored the field would
        # place a BARE entry while every layer above believed the position was
        # protected from the first instant — strictly worse than the sweep it
        # replaces, because the sweep at least knows it has work to do. Refuse
        # loudly instead; the caller checks ``supports_attached_stop`` first.
        if attached_stop_price is not None and not self._broker.supports_attached_stop():
            raise ValueError(
                f"{self._broker_name.value} cannot place an attached stop for "
                f"{symbol}; refusing to place an unprotected entry"
            )

        # 1. Kill switch. ``allow_when_killed`` is reserved for the breaker
        # flatten path (Decision 021): position-REDUCING orders may pass an
        # engaged kill switch; risk-increasing orders never can.
        if not (allow_when_killed and reduce_only):
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
        # ``extra_payload`` carries per-order facts the placement itself cannot
        # derive and later stages need off the ledger: the strategy's protective
        # stop distance (read by the stop sweep — Decision 032) and the margin
        # the sizer allotted (read by the MTF carry-interest charge). Never
        # secrets; this row is dashboard-visible.
        extra: dict[str, Any] = dict(extra_payload or {})
        # Decision 036 — WHICH PRODUCT this order was sent as. Not recorded
        # before, and its absence is a real hole rather than a nicety: charges
        # differ per product (delivery STT is 0.1% both sides, intraday 0.025%
        # sell-only, F&O different again), so without it the fee-card drift
        # check has to GUESS a trade's segment from its bucket. That guess is
        # wrong precisely when the Decision 031 CNC fallback fires — a bucket
        # configured INTRADAY whose order actually went as CNC — which is the
        # case a cost check most needs to get right.
        #
        # Records what was ASKED for. An adapter-side fallback can still change
        # it at the venue, which the reconciler sees in the charges themselves;
        # the point is to stop guessing the starting point too.
        if product:
            extra["product"] = product
        # Decision 036 — stamped ALWAYS, both True and False, not only when
        # True. Ownership needs to tell "this order opens exposure" from "this
        # row predates the flag", and an absent key cannot say which. With only
        # the True case stamped, a sell-to-OPEN (the options bucket's entry) is
        # indistinguishable from a legacy row and falls back to the long-only
        # rule "a SELL closes" — so the bot would not recognise its own naked
        # short between placement and fill, which is exactly the window the
        # early-recognition rule exists to cover.
        #
        # Safe for every existing path: entries are BUYs (False, which the side
        # rule already concluded) and every SELL in this repo today is
        # reduce_only=True, so no live behaviour changes.
        extra["reduce_only"] = bool(reduce_only)
        if attached_stop_price is not None:
            # The ledger must record that this entry carries its OWN protection,
            # so the sweep does not plan a second stop for it and the invariant
            # can tell a covered position from a naked one.
            extra["attached_stop"] = str(attached_stop_price)
            extra["super_order"] = True
            if attached_target_price is not None:
                extra["attached_target"] = str(attached_target_price)
        if stop_price is not None:
            extra["stop_price"] = str(stop_price)
            if reduce_only:
                # Protective stop (Decision 022) — excluded from the exit
                # engine's in-flight-exit dedup; pairs as a real exit in
                # P&L enrichment only once it actually fills.
                extra["protective_stop"] = True
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
            extra=extra or None,
        )
        with session_scope() as session:
            session.add(trade)
            session.flush()
            trade_id = trade.id

        # 4. Set leverage. A failure ABORTS the placement: firing a market
        # order at whatever leverage the account happens to carry means the
        # margin consumed can be a multiple of what the sizer assumed.
        if leverage is not None:
            try:
                self._broker.set_leverage(symbol, leverage)
            except Exception:
                self._log.error(
                    "set_leverage_failed_aborting",
                    symbol=symbol,
                    leverage=str(leverage),
                    exc_info=True,
                )
                self._mark_rejected(
                    trade_id, strategy_id, client_oid,
                    f"set_leverage({leverage}) failed for {symbol}",
                )
                raise

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
                    stop_price=stop_price,
                    attached_stop_price=attached_stop_price,
                    attached_target_price=attached_target_price,
                    product=product,
                    fallback_max_size=fallback_max_size,
                )
            )
        except AttachedStopRetireError:
            # Decision 034: the adapter refused to send this CLOSING order
            # because it could not first retire the venue-side stop leg that
            # would otherwise outlive the position. Nothing reached the venue,
            # so the recovery lookup below would burn a rate-limited day-book
            # scan to learn nothing, and REJECTED would be a lie that feeds
            # check_reject_rate straight into a bucket halt.
            #
            # CANCELED is the honest status: we chose not to send it. The exit
            # is retried on the next tick, and until it succeeds the position
            # stays open AND stays protected by the very leg we could not
            # cancel — the safe resting state.
            self._mark_canceled(
                trade_id, strategy_id, client_oid, "attached_stop_retire_failed"
            )
            raise
        except Exception:
            # The request may have DIED IN TRANSIT after the exchange
            # accepted it (e.g. response timeout). Marking it REJECTED in
            # that case would let the next tick fire a duplicate under a
            # fresh minute-based client_order_id. Ask the exchange first.
            recovered = self._lookup_after_error(client_oid)
            if recovered is not None:
                mapped = _map_broker_status(recovered.status)
                with session_scope() as session:
                    t = session.get(Trade, trade_id)
                    if t:
                        t.exchange_order_id = recovered.exchange_order_id
                        t.status = mapped
                self._log.warning(
                    "order_recovered_after_transport_error",
                    client_order_id=client_oid,
                    exchange_order_id=recovered.exchange_order_id,
                    status=mapped.value,
                )
                send_alert(
                    f"[{bucket_id or strategy_id}] order {symbol} recovered after "
                    f"transport error [{mapped.value}] — no duplicate fired"
                )
                return PlacementResult(
                    trade_id=trade_id,
                    client_order_id=client_oid,
                    exchange_order_id=recovered.exchange_order_id,
                    status=mapped,
                    was_existing=False,
                    raw=recovered.raw,
                )
            self._mark_rejected(
                trade_id, strategy_id, client_oid, "broker_error"
            )
            raise

        # 6. Update trade with exchange response
        mapped_status = _map_broker_status(result.status)
        with session_scope() as session:
            t = session.get(Trade, trade_id)
            if t:
                t.exchange_order_id = result.exchange_order_id
                t.status = mapped_status
                # An adapter may place LESS than requested — Dhan clamps an
                # MIS→CNC fallback to the 1x-affordable size (Decision 029,
                # amended). The row must record what the exchange actually
                # holds: ownership scoping, P&L and stop sizing all read it.
                if result.size and result.size != size:
                    self._log.warning(
                        "placed_size_differs_from_request",
                        client_order_id=client_oid,
                        symbol=symbol,
                        requested=str(size),
                        placed=str(result.size),
                    )
                    t.quantity = result.size
                # Decision 034: whether the mandatory target leg was actually
                # retired. False means a live take-profit is resting at a price
                # no backtest justifies (House Rule 7) — the sweep retries it,
                # and this flag is how anyone reading the ledger can tell.
                if "_target_leg_cancelled" in result.raw:
                    t.extra = {
                        **(t.extra or {}),
                        "target_leg_cancelled": bool(
                            result.raw["_target_leg_cancelled"]
                        ),
                    }
                # The venue's reason, when the adapter resolved the order far
                # enough to have one. Broker-agnostic: any adapter that can name
                # a rejection puts it under this key. The reconciler stores the
                # same key for the async case (an order rejected after we stop
                # looking), so one query answers "why was this refused" whichever
                # path found out.
                if result.raw.get("_reject_reason"):
                    t.extra = {
                        **(t.extra or {}),
                        "reject_reason": str(result.raw["_reject_reason"]),
                    }
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

        scope = bucket_id or strategy_id
        tag = "STOP" if stop_price is not None else ("EXIT" if reduce_only else "ORDER")
        if stop_price is not None:
            price_str = f"trigger {stop_price}"
        else:
            price_str = str(limit_price) if limit_price is not None else "market"
        send_alert(
            f"[{scope}] {tag} {side.upper()} {size} {symbol} @ {price_str} "
            f"[{mapped_status.value}]"
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

    def _lookup_after_error(self, client_oid: str) -> Any:
        """Best-effort exchange lookup by client_order_id after a transport
        error. Returns the broker's order record if the order actually
        landed, else None (including when the lookup itself fails)."""
        try:
            return self._broker.get_order_by_client_id(client_oid)
        except Exception:
            self._log.warning(
                "post_error_order_lookup_failed",
                client_order_id=client_oid,
                exc_info=True,
            )
            return None

    def _mark_canceled(
        self, trade_id: int, strategy_id: str, client_oid: str, reason: str
    ) -> None:
        """Record an order the bot DECIDED not to send (Decision 034).

        Deliberately not REJECTED: nothing was refused by the venue, and the
        reject-rate invariant must keep meaning "the venue is refusing us".
        """
        with session_scope() as session:
            t = session.get(Trade, trade_id)
            if t:
                t.status = OrderStatus.CANCELED
                t.extra = {**(t.extra or {}), "not_sent_reason": reason}
            session.add(
                AuditLog(
                    strategy_id=strategy_id,
                    event_type=AuditEventType.ORDER_CANCELED,
                    message=f"NOT SENT {client_oid}: {reason}",
                    payload={"client_order_id": client_oid, "reason": reason},
                )
            )

    def _mark_rejected(
        self, trade_id: int, strategy_id: str, client_oid: str, reason: str
    ) -> None:
        with session_scope() as session:
            t = session.get(Trade, trade_id)
            if t:
                t.status = OrderStatus.REJECTED
            session.add(
                AuditLog(
                    strategy_id=strategy_id,
                    event_type=AuditEventType.ORDER_PLACED,
                    message=f"REJECTED {client_oid}",
                    payload={"client_order_id": client_oid, "reason": reason},
                )
            )

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
        "partial": OrderStatus.PARTIAL,
        "canceled": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED,
    }.get(status_str, OrderStatus.UNKNOWN)
