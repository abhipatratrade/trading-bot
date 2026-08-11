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
from src.core.models import OrderStatus, Position, PositionSide, Trade
from src.order_manager.manager import OrderManager
from src.order_manager.ownership import bot_owned_quantities

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


def expected_trigger_at_distance(
    entry_price: Decimal,
    position_side: str,
    distance: Decimal,
    tick: Decimal | None = None,
) -> Decimal:
    """Trigger an absolute ``distance`` (quote currency) away from entry.

    The percent form above is a bucket-wide crash net. Some strategies size the
    stop off the instrument's own volatility instead — swing-indian's 1h mean
    reversion rests it at ``entry − 3.5 × daily ATR14`` (Decision 032), which is
    the stop its backtest was validated with. The strategy supplies the rupee
    distance at entry; it is fixed for the life of the position, exactly as the
    backtest fixes it at the entry bar.
    """
    if position_side == "long":
        raw = entry_price - distance
    else:
        raw = entry_price + distance
    return _round_to_tick(raw, tick)


def plan_stop_protection(
    *,
    positions: list[PositionInfo],
    open_orders: list[OpenOrder],
    stop_pct_by_bucket: dict[str, Decimal],
    attribution: dict[str, tuple[str | None, str | None]],
    tick_sizes: dict[str, Decimal | None] | None = None,
    owned_quantities: dict[str, Decimal] | None = None,
    stop_distances: dict[str, Decimal] | None = None,
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
        stop_distances: ``symbol → absolute distance from entry`` supplied by
            the strategy that opened the position (Decision 032). Takes
            precedence over the bucket percent for those symbols; a distance
            that is missing, non-positive, or wider than the bucket's own
            crash net is ignored, so a bad number can only ever make the stop
            tighter than the configured worst case, never looser.
        owned_quantities: SHARED-account guard (Decision 027 followup). When
            provided (Dhan — the account also holds the user's manual trades),
            ONLY symbols present here are the bot's; any other position is the
            user's and is left completely alone — no stop placed, and its own
            resting stops never cancelled. The stop is also sized to the bot's
            own quantity, so an overlapping user holding is never covered. When
            None (crypto — exclusive sub-account, Decision 019), every position
            is the bot's and behaviour is unchanged.
    """
    plan = StopPlan()
    ticks = tick_sizes or {}
    shared = owned_quantities is not None

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
        # Shared account: skip anything the bot didn't open. Crucially do NOT
        # pop its resting stops — leave the user's own stops untouched.
        if shared and pos.symbol not in owned_quantities:  # type: ignore[operator]
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

        # Never protect more than the bot's own quantity (overlap guard).
        size = pos.size
        if shared:
            size = min(pos.size, owned_quantities[pos.symbol])  # type: ignore[index]
        if size <= 0:
            continue

        want_side = "sell" if pos.side == "long" else "buy"
        tick = ticks.get(pos.symbol)
        trigger = expected_trigger(pos.entry_price, pos.side, pct, tick)
        distance = (stop_distances or {}).get(pos.symbol)
        if distance is not None and distance > 0:
            strategy_trigger = expected_trigger_at_distance(
                pos.entry_price, pos.side, distance, tick
            )
            # Only ever tighten: the bucket pct is the guaranteed worst case.
            inside = (
                strategy_trigger > trigger
                if pos.side == "long"
                else strategy_trigger < trigger
            )
            if inside and strategy_trigger > 0:
                trigger = strategy_trigger
            else:
                _log.warning(
                    "stop_distance_ignored_wider_than_bucket_net",
                    symbol=pos.symbol,
                    strategy_trigger=str(strategy_trigger),
                    bucket_trigger=str(trigger),
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
                and o.size == size
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
                    size=size,
                    trigger=trigger,
                    bucket_id=bucket_id,
                    strategy_name=strategy_name,
                )
            )

    # Whatever is left has no position behind it → orphan, cancel. On a shared
    # account only cancel the bot's own orphaned stops; a resting stop on a
    # symbol the bot doesn't own belongs to the user.
    for sym, leftovers in stops_by_symbol.items():
        if shared and sym not in owned_quantities:  # type: ignore[operator]
            continue
        plan.cancel.extend(leftovers)

    return plan


def _load_attribution(
    bucket_ids: list[str],
) -> dict[str, tuple[str | None, str | None]]:
    """symbol → (bucket_id, strategy_name), from Position then the entry Trade.

    Position is authoritative but LATE. Those rows are written by the
    reconciler, which sweeps every 5 minutes — and the stop sweep runs seconds
    after a fill, so on the one entry where the stop matters most this table is
    still empty. An unattributed symbol then falls through to
    ``bucket_ids[0]`` in ``ensure_stop_protection``, which sends the stop under
    the WRONG bucket's product.

    That is not hypothetical. On 2026-08-11 intraday-indian filled CASTROLIND
    (product INTRADAY) at 09:40:40 and the sweep tried to protect it 9 seconds
    later. No Position row existed, so it resolved to swing-indian and sent the
    stop as MTF. Dhan rejected it, and kept rejecting it 116 times over 5.5
    hours while a live ₹50k position sat with no resting stop.

    So fall back to the entry Trade, the same shape ``_load_stop_distances``
    uses below. Same lesson the sizer's dedup gate learned from the 2026-06-12
    duplicate-order bug: between placement and the next reconcile, Trade is the
    only record that the position exists.

    PENDING IS IN THE STATUS FILTER ON PURPOSE. A Dhan order is ``pending`` the
    moment it is placed — ``OrderManager.place_order`` writes PENDING and then
    overwrites it with the broker ack, and Dhan's ``_STATUS_MAP`` maps both
    TRANSIT and PENDING to ``pending``. CASTROLIND was still PENDING a full
    minute after its fill. A filter of FILLED/PARTIAL/OPEN would therefore miss
    the exact row this fallback exists to find, which is how the first draft of
    this fix was inert. Attributing a PENDING order that later rejects is
    harmless: ``plan_stop_protection`` only ever looks up symbols the BROKER
    reports as open positions, so an entry that never filled is never consulted.

    Position still wins where it exists — it reflects the exchange, whereas a
    Trade row only reflects what we asked for. A NULL-bucket Position row (the
    reconciler's orphan import on a shared account) simply does not match the
    filter, so the Trade fallback covers it too.
    """
    with session_scope() as session:
        trades = (
            session.execute(
                select(Trade)
                .where(
                    Trade.bucket_id.in_(bucket_ids),
                    Trade.status.in_(
                        [
                            OrderStatus.PENDING,
                            OrderStatus.OPEN,
                            OrderStatus.PARTIAL,
                            OrderStatus.FILLED,
                        ]
                    ),
                )
                .order_by(Trade.created_at.desc())
                .limit(500)
            )
            .scalars()
            .all()
        )
        positions = list(
            session.execute(
                select(Position).where(
                    Position.bucket_id.in_(bucket_ids),
                    Position.side != PositionSide.FLAT,
                )
            ).scalars()
        )
        return merge_attribution(
            [(t.symbol, t.bucket_id, t.strategy_name, t.extra or {}) for t in trades],
            [(p.symbol, p.bucket_id, p.strategy_name) for p in positions],
        )


def merge_attribution(
    trades: list[tuple[str, str | None, str | None, dict]],
    positions: list[tuple[str, str | None, str | None]],
) -> dict[str, tuple[str | None, str | None]]:
    """Resolve symbol → (bucket, strategy) from both records. PURE.

    ``trades`` must arrive NEWEST FIRST; the first unpaired entry per symbol
    wins, mirroring ``_load_stop_distances``. Positions are applied last so a
    reconciled row overrides the Trade guess.
    """
    out: dict[str, tuple[str | None, str | None]] = {}
    for symbol, bucket_id, strategy_name, extra in trades:
        # A reduce-only leg is an EXIT — it attributes nothing, and letting it
        # win would name the strategy that closed the position rather than the
        # one that holds it. A closed entry is no longer held at all.
        if symbol in out or extra.get("reduce_only"):
            continue
        if extra.get("closed_by_trade_id"):
            continue
        out[symbol] = (bucket_id, strategy_name)
    for symbol, bucket_id, strategy_name in positions:
        out[symbol] = (bucket_id, strategy_name)
    return out


def _load_stop_distances(bucket_ids: list[str]) -> dict[str, Decimal]:
    """symbol → protective-stop distance stamped on its open entry Trade.

    Strategies that own their stop distance (Decision 032) put it on the entry
    order via ``OrderManager.place_order(extra_payload=...)``. The newest
    unpaired entry per symbol wins; anything unparseable is dropped so the
    sweep silently falls back to the bucket percent.
    """
    out: dict[str, Decimal] = {}
    with session_scope() as session:
        rows = (
            session.execute(
                select(Trade)
                .where(
                    Trade.bucket_id.in_(bucket_ids),
                    Trade.status.in_(
                        [OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.OPEN]
                    ),
                )
                .order_by(Trade.created_at.desc())
                .limit(500)
            )
            .scalars()
            .all()
        )
        for t in rows:
            extra = t.extra or {}
            if t.symbol in out or extra.get("reduce_only"):
                continue
            if extra.get("closed_by_trade_id"):
                continue
            raw = extra.get("stop_distance")
            if raw is None:
                continue
            try:
                value = Decimal(str(raw))
            except (ArithmeticError, ValueError):
                continue
            if value > 0:
                out[t.symbol] = value
    return out


def ensure_stop_protection(
    *,
    account_ref: str,
    bucket_ids: list[str],
    broker: Broker,
    order_manager: OrderManager,
    stop_pct_by_bucket: dict[str, Decimal],
    product_by_bucket: dict[str, str] | None = None,
    clock: Clock | None = None,
    shared_account: bool = False,
) -> StopPlan:
    """Make the exchange state match the plan for one sub-account.

    Returns the executed plan (for logging/tests). Failures on one symbol
    never block the rest; an unprotected position pages via Telegram.

    ``shared_account`` (Decision 027 followup): on the Dhan account, which also
    holds the user's manual positions, only stops the bot's OWN holdings — the
    user's positions and their resting stops are never touched.

    ``product_by_bucket`` is what makes the attribution above MATTER. Without
    it ``place_order`` omits ``product``, ``OrderRequest.product`` is None, and
    ``DhanClient`` falls back to its constructor default — MTF — so every stop
    went out as MTF no matter which bucket held the position. A reduce-only MTF
    sell against an INTRADAY long does not reduce it; on 2026-08-11 Dhan
    rejected it 116 times, and now that MTF consent exists an accepted one
    would be worse than a rejected one. The stop must carry the SAME product as
    the entry it protects.
    """
    clk = clock or RealClock()
    if not stop_pct_by_bucket:
        return StopPlan()  # no bucket on this account wants stops

    positions = broker.get_positions()
    open_orders = broker.get_open_orders()
    owned = None
    if shared_account:
        with session_scope() as session:
            owned = bot_owned_quantities(
                session,
                broker_name=order_manager.broker_name,
                bucket_ids=bucket_ids,
                now=clk.now(),
            )
    plan = plan_stop_protection(
        positions=positions,
        open_orders=open_orders,
        stop_pct_by_bucket=stop_pct_by_bucket,
        attribution=_load_attribution(bucket_ids),
        tick_sizes={p.symbol: broker.tick_size(p.symbol) for p in positions},
        owned_quantities=owned,
        stop_distances=_load_stop_distances(bucket_ids),
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
                # Must match the product the position is HELD under, or the
                # "reduce-only" sell lands in a different product bucket at the
                # venue and protects nothing. None ⇒ the adapter's default,
                # which is the pre-2026-08-12 behaviour.
                product=(product_by_bucket or {}).get(scope),
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
