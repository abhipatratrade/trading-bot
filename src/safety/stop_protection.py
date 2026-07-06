"""
Broker-side protective stop-losses (Decision 022).

Every open position must have an exchange-resident reduce-only
stop-market order at ``stop_loss_pct`` (from ``buckets.yaml``) away from
its entry price. Because the stop rests ON the exchange, the max loss per
position holds even when the bot or its VM is down — the biggest risk gap
identified in the 2026-07-06 review.

``run_bot`` calls :func:`ensure_stop_protection` once per tick per
sub-account, after the bucket runners (so entries placed this tick are
protected seconds later) and once at startup. The sweep is idempotent and
self-healing:

    - position without a stop            → place one
    - stop size ≠ position size (adds)   → cancel + re-place at full size
    - stop trigger drifted from expected → cancel + re-place
    - stop without a position (orphan)   → cancel

Stops go through :class:`OrderManager` so each gets a Trade row: if a
stop fires while the bot is down, the reconciler marks it FILLED on the
next boot and P&L enrichment pairs it with its entry like any other exit.
``allow_when_killed`` is set — a protective stop is risk-REDUCING, so it
must be maintained even while a kill switch is engaged (same rationale as
the Decision 021 flatten path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from src.brokers.base import Broker, OpenOrder, OrderType, PositionInfo
from src.core.alerts import send_alert_dedup
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import Position, PositionSide
from src.order_manager.manager import OrderManager

_log = get_logger("safety.stop_protection")

# Re-place the stop when its trigger sits more than this relative distance
# from the expected trigger (entry price moved on adds, config changed).
_TRIGGER_TOLERANCE = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class PlannedStop:
    """One stop-market order the sweep wants resting on the exchange."""

    symbol: str
    side: str  # "sell" closes a long, "buy" closes a short
    size: Decimal
    trigger: Decimal
    bucket_id: str | None
    strategy_name: str | None


@dataclass(slots=True)
class StopPlan:
    """Actions computed by :func:`plan_stop_protection` (pure, no I/O)."""

    place: list[PlannedStop] = field(default_factory=list)
    cancel: list[OpenOrder] = field(default_factory=list)
    unprotectable: list[str] = field(default_factory=list)  # no pct configured


def _round_to_tick(price: Decimal, tick: Decimal | None) -> Decimal:
    if tick is None or tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def expected_trigger(
    entry_price: Decimal,
    position_side: str,
    stop_pct: Decimal,
    tick: Decimal | None = None,
) -> Decimal:
    """Trigger price ``stop_pct`` percent away from entry, snapped to tick.

    Longs stop below entry, shorts stop above.
    """
    frac = stop_pct / Decimal("100")
    if position_side == "long":
        raw = entry_price * (Decimal("1") - frac)
    else:
        raw = entry_price * (Decimal("1") + frac)
    return _round_to_tick(raw, tick)


def plan_stop_protection(
    *,
    positions: list[PositionInfo],
    open_orders: list[OpenOrder],
    stop_pct_by_bucket: dict[str, Decimal],
    attribution: dict[str, tuple[str | None, str | None]],
    tick_sizes: dict[str, Decimal | None] | None = None,
) -> StopPlan:
    """Diff exchange positions against resting protective stops.

    Args:
        positions: live positions on this sub-account.
        open_orders: live open/pending orders on this sub-account.
        stop_pct_by_bucket: ``bucket_id → stop_loss_pct`` for the buckets on
            this account (only buckets with a pct configured).
        attribution: ``symbol → (bucket_id, strategy_name)`` from DB
            Position rows; unattributed symbols fall back to the account's
            most conservative (smallest) pct.
        tick_sizes: ``symbol → tick`` for trigger snapping (None ⇒ no snap).
    """
    plan = StopPlan()
    ticks = tick_sizes or {}

    # Resting protective stops, grouped by symbol. Only reduce-only stop
    # orders count — a strategy's own resting stop entry (none exist today)
    # would not be reduce-only.
    stops_by_symbol: dict[str, list[OpenOrder]] = {}
    for o in open_orders:
        if o.stop_price is not None and o.reduce_only:
            stops_by_symbol.setdefault(o.symbol, []).append(o)

    fallback_pct = (
        min(stop_pct_by_bucket.values()) if stop_pct_by_bucket else None
    )

    for pos in positions:
        if pos.size <= 0 or pos.side not in ("long", "short"):
            continue
        existing = stops_by_symbol.pop(pos.symbol, [])

        bucket_id, strategy_name = attribution.get(pos.symbol, (None, None))
        pct = (
            stop_pct_by_bucket.get(bucket_id) if bucket_id else None
        ) or fallback_pct
        if pct is None:
            plan.unprotectable.append(pos.symbol)
            # Leave any existing stops alone — better a stale stop than none.
            continue

        want_side = "sell" if pos.side == "long" else "buy"
        trigger = expected_trigger(
            pos.entry_price, pos.side, pct, ticks.get(pos.symbol)
        )

        kept = None
        for o in existing:
            drift = (
                abs(o.stop_price - trigger) / trigger
                if trigger > 0
                else Decimal("0")
            )
            if (
                kept is None
                and o.side == want_side
                and o.size == pos.size
                and drift <= _TRIGGER_TOLERANCE
            ):
                kept = o
            else:
                plan.cancel.append(o)

        if kept is None:
            plan.place.append(
                PlannedStop(
                    symbol=pos.symbol,
                    side=want_side,
                    size=pos.size,
                    trigger=trigger,
                    bucket_id=bucket_id,
                    strategy_name=strategy_name,
                )
            )

    # Whatever is left has no position behind it → orphan, cancel.
    for leftovers in stops_by_symbol.values():
        plan.cancel.extend(leftovers)

    return plan


def _load_attribution(
    bucket_ids: list[str],
) -> dict[str, tuple[str | None, str | None]]:
    """symbol → (bucket_id, strategy_name) from open DB Position rows."""
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Position).where(
                    Position.bucket_id.in_(bucket_ids),
                    Position.side != PositionSide.FLAT,
                )
            ).scalars()
        )
        return {p.symbol: (p.bucket_id, p.strategy_name) for p in rows}


def ensure_stop_protection(
    *,
    account_ref: str,
    bucket_ids: list[str],
    broker: Broker,
    order_manager: OrderManager,
    stop_pct_by_bucket: dict[str, Decimal],
    clock: Clock | None = None,
) -> StopPlan:
    """Make the exchange state match the plan for one sub-account.

    Returns the executed plan (for logging/tests). Failures on one symbol
    never block the rest; an unprotected position pages via Telegram.
    """
    clk = clock or RealClock()
    if not stop_pct_by_bucket:
        return StopPlan()  # no bucket on this account wants stops

    positions = broker.get_positions()
    open_orders = broker.get_open_orders()
    plan = plan_stop_protection(
        positions=positions,
        open_orders=open_orders,
        stop_pct_by_bucket=stop_pct_by_bucket,
        attribution=_load_attribution(bucket_ids),
        tick_sizes={p.symbol: broker.tick_size(p.symbol) for p in positions},
    )

    fallback_bucket = bucket_ids[0] if bucket_ids else "unknown"

    # Cancels first so a replace never briefly doubles the stop size.
    for order in plan.cancel:
        try:
            order_manager.cancel_order(
                strategy_id=fallback_bucket,
                symbol=order.symbol,
                exchange_order_id=order.exchange_order_id,
            )
        except Exception:
            _log.error(
                "stop_cancel_failed",
                account_ref=account_ref,
                symbol=order.symbol,
                exchange_order_id=order.exchange_order_id,
                exc_info=True,
            )

    minute = clk.now().strftime("%Y%m%d%H%M")
    for stop in plan.place:
        scope = stop.bucket_id or fallback_bucket
        try:
            order_manager.place_order(
                strategy_id=scope,
                bucket_id=stop.bucket_id,
                strategy_name=stop.strategy_name or "stop_protection",
                symbol=stop.symbol,
                side=stop.side,
                size=stop.size,
                order_type=OrderType.MARKET,
                reduce_only=True,
                stop_price=stop.trigger,
                allow_when_killed=True,
                intent_id=f"stop-{stop.trigger}-{stop.size}-{minute}",
            )
        except Exception:
            _log.error(
                "stop_place_failed",
                account_ref=account_ref,
                symbol=stop.symbol,
                trigger=str(stop.trigger),
                exc_info=True,
            )
            send_alert_dedup(
                f"stop_unprotected:{account_ref}:{stop.symbol}",
                f"[{scope}] {stop.symbol} position has NO protective stop "
                f"(placement failed) — check exchange",
            )

    for sym in plan.unprotectable:
        send_alert_dedup(
            f"stop_no_pct:{account_ref}:{sym}",
            f"[{account_ref}] {sym} position cannot be stop-protected — "
            f"no stop_loss_pct configured for its bucket",
        )

    if plan.place or plan.cancel:
        _log.info(
            "stop_protection_swept",
            account_ref=account_ref,
            placed=[s.symbol for s in plan.place],
            canceled=[o.symbol for o in plan.cancel],
        )
    return plan
