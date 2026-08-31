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
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from src.brokers.base import Broker, OpenOrder, OrderType, PositionInfo
from src.core.alerts import send_alert_dedup
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    OrderSide,
    OrderStatus,
    Position,
    PositionSide,
    Trade,
)
from src.order_manager.manager import OrderManager
from src.order_manager.ownership import bot_owned_quantities

_log = get_logger("safety.stop_protection")

# Re-place the stop when its trigger sits more than this relative distance
# from the expected trigger (entry price moved on adds, config changed).
_TRIGGER_TOLERANCE = Decimal("0.005")

# Fraction of the daily circuit band a clamped stop may use. Not 1.0: the band
# is measured from the PREVIOUS CLOSE, while we only have the entry price, so a
# position already down on the day has less room left than entry × band
# suggests. 0.9 buys margin for that drift without giving up much distance.
_BAND_SAFETY = Decimal("0.9")

# Target distance used when the scrip's circuit band is unknown (Decision 034).
# 4% is inside even a 5% band, the tightest NSE applies. See
# ``resolve_target_price`` for why erring tight is the safe direction.
_FALLBACK_TARGET_PCT = Decimal("4")

# How long after placing an entry the orphan-leg pass refuses to call its stop
# leg orphaned. Covers the window where the venue holds the leg but has not yet
# reported the position. Minutes, not seconds: a sweep runs every 60s, and one
# extra sweep before retiring a genuinely dead leg costs nothing, while acting
# one sweep too early strips a live position of its only stop.
_ENTRY_GRACE_MINUTES = 5

# Consecutive placement failures for one (account, symbol, trigger) before the
# sweep stops retrying it. A stop the venue refuses is refused deterministically
# — the 117 identical PIIND attempts of 2026-08-12 taught nothing after the
# first and just burned API budget on an account that is rate-limited and shared
# with the user's manual trading. The position stays uncovered either way; that
# is what stop_coverage exists to shout about, and it keeps shouting.
#
# Keyed on the TRIGGER too, so a changed price (band clamp, strategy distance
# arriving late, entry re-priced) is a genuinely new attempt and gets its own
# budget rather than inheriting the old one's exhaustion.
_MAX_PLACE_FAILURES = 3
_place_failures: dict[tuple[str, str, str], int] = {}


def reset_place_failures(key: tuple[str, str, str] | None = None) -> None:
    """Clear the retry budget — for tests, and for a symbol that recovered."""
    if key is None:
        _place_failures.clear()
    else:
        _place_failures.pop(key, None)


def should_attempt_place(
    account_ref: str, symbol: str, trigger: Decimal, cap: int = _MAX_PLACE_FAILURES
) -> bool:
    """False once this exact stop has failed ``cap`` times in a row. PURE-ish.

    Only the counter is stateful; the decision is a plain comparison so the
    policy is testable without a broker.
    """
    return _place_failures.get((account_ref, symbol, str(trigger)), 0) < cap


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
    # Positions the VENUE already protects via an attached stop leg (Decision
    # 034). The sweep deliberately does nothing for these — recorded so
    # "did nothing" is visible in the logs as a decision rather than a gap.
    attached: list[str] = field(default_factory=list)
    # Attached stop legs with NO position behind them any more. These must be
    # retired: a stop leg that outlives its position sells stock we no longer
    # hold, which on MTF is a short. The adapter's cancel-before-close guard
    # cannot catch these because nothing the bot did closed them — Dhan's MIS
    # auto-square-off, a manual close by the user on this shared account, or
    # the target leg filling all end the position without passing through
    # ``place_order``.
    retire_legs: list[str] = field(default_factory=list)


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


def resolve_stop_trigger(
    *,
    entry_price: Decimal,
    position_side: str,
    stop_pct: Decimal,
    distance: Decimal | None = None,
    band_pct: Decimal | None = None,
    tick: Decimal | None = None,
    symbol: str = "",
) -> Decimal:
    """The protective trigger this position should carry. PURE.

    Three inputs, applied in a strict order that can only ever TIGHTEN:

      1. the bucket's ``stop_loss_pct`` — the guaranteed crash net;
      2. the strategy's own distance (Decision 032), used only if it sits
         INSIDE the net, so a bad number can never widen the worst case;
      3. the scrip's daily circuit band, which is not a preference but a
         placement constraint — a trigger outside it is refused by the
         exchange at validation, so the position ends up with NO stop rather
         than a wide one (PIIND, 2026-08-12).

    Extracted so the Decision 022 sweep and the Decision 034 attached stop
    compute the SAME number. They were briefly going to own separate copies of
    this arithmetic, which is exactly how ``_load_attribution`` and
    ``_load_stop_distances`` drifted apart and cost a live position its stop.
    """
    trigger = expected_trigger(entry_price, position_side, stop_pct, tick)

    if distance is not None and distance > 0:
        strategy_trigger = expected_trigger_at_distance(
            entry_price, position_side, distance, tick
        )
        inside = (
            strategy_trigger > trigger
            if position_side == "long"
            else strategy_trigger < trigger
        )
        if inside and strategy_trigger > 0:
            trigger = strategy_trigger
        else:
            _log.warning(
                "stop_distance_ignored_wider_than_bucket_net",
                symbol=symbol,
                strategy_trigger=str(strategy_trigger),
                bucket_trigger=str(trigger),
            )

    if band_pct and band_pct > 0 and entry_price > 0:
        limit = entry_price * (
            Decimal("1") - (band_pct * _BAND_SAFETY) / Decimal("100")
            if position_side == "long"
            else Decimal("1") + (band_pct * _BAND_SAFETY) / Decimal("100")
        )
        outside = limit > trigger if position_side == "long" else limit < trigger
        if outside:
            _log.warning(
                "stop_trigger_clamped_to_price_band",
                symbol=symbol,
                requested=str(trigger),
                clamped_to=str(limit),
                band_pct=str(band_pct),
            )
            trigger = expected_trigger_at_distance(
                entry_price, position_side, abs(entry_price - limit), tick
            )

    return trigger


def resolve_target_price(
    *,
    entry_price: Decimal,
    position_side: str,
    band_pct: Decimal | None = None,
    tick: Decimal | None = None,
) -> Decimal:
    """A take-profit we intend to CANCEL, priced so it cannot do harm. PURE.

    Dhan makes ``targetPrice`` mandatory on a super order (Decision 034) and no
    strategy here has a target, so the leg is cancelled immediately after
    placement. This price only has to survive the moments before that, and one
    failed cancel.

    The naive choice — a target so far away it can never fill — is the wrong
    one, and for the exact reason the stop clamp exists: a price outside the
    scrip's daily circuit band is refused at validation, and here that would
    reject the WHOLE super order, entry included. A stop we could not place
    cost us a protected position; a target we cannot place would cost us the
    trade.

    So it sits just inside the band (the same ``_BAND_SAFETY`` margin the stop
    uses), which is the furthest away it can legally be. When the band is
    unknown we fall back to ``_FALLBACK_TARGET_PCT`` — deliberately tighter
    than any real band, because being wrong towards "too close" only risks an
    unwanted profit-take on a cancel we already failed to make, while being
    wrong towards "too far" kills the entry outright.
    """
    pct = (
        band_pct * _BAND_SAFETY
        if band_pct and band_pct > 0
        else _FALLBACK_TARGET_PCT
    )
    frac = pct / Decimal("100")
    if position_side == "long":
        raw = entry_price * (Decimal("1") + frac)
    else:
        raw = entry_price * (Decimal("1") - frac)
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
    price_band_pct: dict[str, Decimal | None] | None = None,
    entry_prices: dict[str, Decimal] | None = None,
    attached_stops: dict[str, Decimal] | None = None,
    recent_entries: set[str] | None = None,
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

        # A SHORT can never be proven ours on a shared account, because the
        # ownership ledger cannot express one: ``net_owned`` returns only
        # positive (long) nets by construction. So a short here is either the
        # user's, or an artifact — and on 2026-08-18 it was an artifact that
        # nearly cost real money.
        #
        # What happened: swing-indian sold its 15 PIIND at 12:16. Selling stock
        # out of HOLDINGS shows up as a negative day-position in Dhan's
        # /v2/positions until settlement catches up, so the broker reported
        # PIIND short 15. The exit order was still PENDING, so ``net_owned``
        # had not yet decremented and the symbol still looked owned — it passed
        # the check above. The sweep then read side=short and planned a BUY
        # stop ABOVE the market to "protect" it.
        #
        # Dhan rejected that one. Had it been accepted, triggering it would have
        # BOUGHT 15 shares — opening a real long position with no strategy
        # behind it, from the module whose entire job is reducing risk.
        if shared and pos.side == "short":
            _log.warning(
                "short_position_not_protected_on_shared_account",
                symbol=pos.symbol,
                size=str(pos.size),
            )
            continue

        # Decision 034: the venue is already holding a stop attached to this
        # entry. Do nothing at all — and POP its orders first, so the leg is
        # not mistaken for an orphan by the cancel pass below. Both halves
        # matter: without the skip the sweep stacks a second stop on top of the
        # venue's every minute, and without the pop it CANCELS the very
        # protection the entry was placed with.
        if pos.symbol in (attached_stops or {}):
            plan.attached.append(pos.symbol)
            # Pop so the cancel pass below cannot treat the venue's own leg as
            # an orphan. But do not discard blindly: anything popped that is
            # OURS and rests at a DIFFERENT price is a second protective stop
            # on one position — a legacy standalone stop left over from a
            # rollback, say. Two resting stops on one long means the second one
            # sells stock the first already sold, i.e. a short. Exactly one
            # protective stop per symbol, always, and the attached one wins.
            want = (attached_stops or {})[pos.symbol]
            for o in stops_by_symbol.pop(pos.symbol, []):
                if o.stop_price is None or want <= 0:
                    continue
                drift = abs(o.stop_price - want) / want
                if drift > _TRIGGER_TOLERANCE:
                    _log.warning(
                        "duplicate_stop_alongside_attached_leg",
                        symbol=pos.symbol,
                        standalone_trigger=str(o.stop_price),
                        attached_trigger=str(want),
                    )
                    plan.cancel.append(o)
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
        # A settled holding reports the BLENDED average across every buy of
        # the scrip, the user's included. Our own ledger entry is the only
        # price this bot's stop should be measured from.
        entry = (entry_prices or {}).get(pos.symbol) or pos.entry_price
        trigger = resolve_stop_trigger(
            entry_price=entry,
            position_side=pos.side,
            stop_pct=pct,
            distance=(stop_distances or {}).get(pos.symbol),
            band_pct=(price_band_pct or {}).get(pos.symbol),
            tick=tick,
            symbol=pos.symbol,
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

    # The same orphan question for VENUE-ATTACHED legs (Decision 034). "Held"
    # here means held BY THE BOT: on a shared account the user may hold the
    # same scrip, and a position row that is entirely theirs must not keep our
    # orphaned leg alive. Every symbol in ``attached_stops`` is already proven
    # ours by the adapter, so anything the bot no longer holds is a leg with
    # nothing behind it.
    bot_held: set[str] = set()
    for pos in positions:
        if pos.size <= 0 or pos.side not in ("long", "short"):
            continue
        if shared and (owned_quantities or {}).get(pos.symbol, Decimal("0")) <= 0:
            continue
        bot_held.add(pos.symbol)
    # ``recent_entries`` is the guard against the race this sweep would
    # otherwise LOSE. Between placing a super order and Dhan surfacing the
    # position, the venue holds a stop leg for a position the broker does not
    # report yet — so "leg with no position" is ALSO what a two-second-old
    # entry looks like. Retiring on that reading cancels the only protection a
    # brand-new position has, which is strictly worse than the bug this whole
    # decision fixes: the pre-034 race merely placed a DUPLICATE stop.
    #
    # A ledger check alone cannot do this job. ``owned_quantities`` counts an
    # entry from PENDING and only decrements on a FILLED sell — so for exactly
    # the cases the orphan pass exists for (Dhan's auto-square-off, a manual
    # close by the user) it would report the symbol as still held forever and
    # the leg would never be retired. The guard has to be TIME-bounded, not
    # existence-bounded.
    plan.retire_legs = sorted(
        set(attached_stops or {}) - bot_held - (recent_entries or set())
    )

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


def _load_entry_prices(bucket_ids: list[str]) -> dict[str, Decimal]:
    """symbol → the price the BOT actually entered at, from its own ledger.

    Needed because a settled holding reports Dhan's ``avgCostPrice``, defined in
    their spec as the average "across full position" — blended over every buy
    of that scrip, including the user's own on this shared account (Decision
    027). If the user holds 100 of a name at 2000 and the bot bought 15 at 2514,
    the broker reports ~2067, and a stop computed from that sits at a price the
    bot's trade never justified.

    Same newest-unpaired-entry rule as the two loaders above, so all three agree
    on what "the open entry" means.
    """
    out: dict[str, Decimal] = {}
    with session_scope() as session:
        rows = (
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
        for t in rows:
            extra = t.extra or {}
            if t.symbol in out or extra.get("reduce_only"):
                continue
            if extra.get("closed_by_trade_id"):
                continue
            # Prefer the actual fill; fall back to the price we asked for.
            raw = extra.get("avg_fill_price") or t.price
            if raw is None:
                continue
            try:
                value = Decimal(str(raw))
            except (ArithmeticError, ValueError):
                continue
            if value > 0:
                out[t.symbol] = value
    return out


def _load_recent_entry_symbols(
    bucket_ids: list[str], now: datetime, within_minutes: int = _ENTRY_GRACE_MINUTES
) -> set[str]:
    """Symbols with a bot ENTRY placed in the last few minutes.

    Sole purpose: keep the orphan-leg pass from cancelling the protection of a
    position the broker has not surfaced yet (see ``plan_stop_protection``).
    Deliberately generous — Dhan reports a filled position within seconds, and
    the cost of being too generous is one sweep's delay in retiring a genuinely
    orphaned leg, while the cost of being too tight is a naked position.

    PENDING is in the filter for the third time in this module, and for the same
    reason as ``_load_attribution`` and ``_load_stop_distances``: a Dhan order is
    ``pending`` from the moment it is placed, so a filter without it would miss
    the exact rows this function exists to find — the newest ones.
    """
    cutoff = now - timedelta(minutes=within_minutes)
    with session_scope() as session:
        rows = (
            session.execute(
                select(Trade.symbol)
                .where(
                    Trade.bucket_id.in_(bucket_ids),
                    Trade.side == OrderSide.BUY,
                    Trade.created_at > cutoff,
                    Trade.status.in_(
                        [
                            OrderStatus.PENDING,
                            OrderStatus.OPEN,
                            OrderStatus.PARTIAL,
                            OrderStatus.FILLED,
                        ]
                    ),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
    return set(rows)


def _load_stop_distances(bucket_ids: list[str]) -> dict[str, Decimal]:
    """symbol → protective-stop distance stamped on its open entry Trade.

    Strategies that own their stop distance (Decision 032) put it on the entry
    order via ``OrderManager.place_order(extra_payload=...)``. The newest
    unpaired entry per symbol wins; anything unparseable is dropped so the
    sweep silently falls back to the bucket percent.

    PENDING IS IN THE FILTER for the same reason as ``_load_attribution`` — and
    this function is why that mattered on 2026-08-12. swing-indian's first ever
    fill, PIIND, emitted its ATR stop distance (200.089 → trigger 2314.41,
    −6.8% from the market). The sweep ran 14 seconds later, the entry Trade was
    still PENDING, this query filtered it out, and the sweep fell back to the
    bucket's 20% → trigger 2011.60, which is −19% and OUTSIDE PIIND's 10%
    circuit band. Dhan refused it at request validation, so the position sat
    naked and the bucket halted itself.

    I fixed this exact filter in ``_load_attribution`` the night before and did
    not fix it here, ten lines away. Both callers of the pattern now agree.
    """
    out: dict[str, Decimal] = {}
    with session_scope() as session:
        rows = (
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
    price_band_pct: dict[str, Decimal | None] | None = None,
    clock: Clock | None = None,
    shared_account: bool = False,
    attached_stops_enabled: bool = False,
    forever_stops_enabled: bool = False,
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

    # Decision 035/037: GTTs rest in a SECOND order book, and the planner below
    # can only cancel an orphan it can see. Merged in here rather than inside
    # ``get_open_orders`` so the reconciler's view of working orders is
    # untouched — a year-long resting trigger is not an order trying to fill.
    #
    # Why a forever orphan is worse than a working one, and why this pass
    # exists at all: a working stop is validity DAY, so an orphan expires by
    # itself — it FAILS SAFE. A forever order rests up to 365 days with no link
    # to any position, so one that outlives its position OPENS a position when
    # it triggers. Nothing else in this codebase would ever retire it.
    #
    # Dark by default, and gated on the FEATURE rather than the capability, for
    # the reason the attached-stop branch below documents: nothing here places a
    # forever order yet, so polling every tick would spend quota on a
    # guaranteed-empty answer — on the account that returned 805 "too many
    # requests" on 2026-08-31. Turn it on in the same change that starts
    # resting them.
    if forever_stops_enabled and hasattr(broker, "supports_forever_orders"):
        try:
            if broker.supports_forever_orders():  # type: ignore[attr-defined]
                open_orders = [
                    *open_orders,
                    *broker.get_forever_orders(),  # type: ignore[attr-defined]
                ]
        except Exception:
            # Does NOT abort the sweep — the opposite of the attached-stop
            # lookup below, and deliberately. There, an empty answer is
            # indistinguishable from "nothing attached" and acting on it would
            # CANCEL live protection, so skipping the tick is the safe move.
            # Here the cost of a failed read is one more tick of an invisible
            # orphan, while aborting would drop standalone stop protection for
            # every position on the account.
            _log.error(
                "forever_order_lookup_failed",
                account_ref=account_ref,
                exc_info=True,
            )
            send_alert_dedup(
                f"forever_lookup:{account_ref}",
                f"[{account_ref}] could not read resting GTTs — an orphaned "
                f"forever stop stays invisible this tick. A GTT that outlives "
                f"its position OPENS one when it triggers.",
            )

    # Decision 034: which positions the venue already protects itself. This is
    # read BEFORE anything is planned and a failure aborts the whole sweep,
    # because an empty answer here is indistinguishable from "nothing is
    # attached" — and acting on that false negative is precisely how the sweep
    # would stack a duplicate stop or cancel a live one. Skipping a tick is
    # safe; the positions in question are protected by the legs we could not
    # enumerate, and the next tick tries again.
    #
    # Gated on the FEATURE being on, not merely on the venue supporting it.
    # ``DhanClient.supports_attached_stop()`` is True the moment this code
    # deploys, so keying off capability alone would send this request every
    # tick on an account where nothing has been enabled — and, because the
    # failure branch below abandons the sweep, an endpoint the account cannot
    # use would SILENTLY DISABLE PROTECTIVE STOPS for both live Indian buckets.
    # A dark deploy has to be genuinely inert.
    attached: dict[str, Decimal] = {}
    if attached_stops_enabled and broker.supports_attached_stop():
        try:
            attached = broker.attached_stop_triggers()
        except Exception:
            _log.error(
                "attached_stop_lookup_failed_skipping_sweep",
                account_ref=account_ref,
                exc_info=True,
            )
            send_alert_dedup(
                f"attached_stop_lookup:{account_ref}",
                f"[{account_ref}] could not read venue-attached stops — stop "
                f"sweep skipped this tick (positions remain protected by their "
                f"own legs)",
            )
            return StopPlan()

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
        price_band_pct=price_band_pct,
        entry_prices=_load_entry_prices(bucket_ids),
        attached_stops=attached,
        recent_entries=(
            _load_recent_entry_symbols(bucket_ids, clk.now()) if attached else None
        ),
    )

    fallback_bucket = bucket_ids[0] if bucket_ids else "unknown"

    # Orphaned attached legs FIRST — before any placement, and before the
    # cancels below. This is the only pass that catches a stop leg whose
    # position ended without the bot closing it (Dhan's MIS auto-square-off,
    # a manual close by the user, the target leg filling). Left resting, such a
    # leg eventually sells stock that is not there.
    for sym in plan.retire_legs:
        try:
            broker.retire_attached_stop(sym)  # type: ignore[attr-defined]
            _log.info(
                "orphan_attached_leg_retired", account_ref=account_ref, symbol=sym
            )
        except Exception:
            _log.error(
                "orphan_attached_leg_retire_failed",
                account_ref=account_ref,
                symbol=sym,
                exc_info=True,
            )
            send_alert_dedup(
                f"orphan_leg:{account_ref}:{sym}",
                f"[{account_ref}] {sym} has a venue stop leg with NO position "
                f"behind it and it could not be cancelled — if it triggers it "
                f"SELLS STOCK YOU DO NOT HOLD. Check the exchange.",
            )

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
        fail_key = (account_ref, stop.symbol, str(stop.trigger))
        if not should_attempt_place(account_ref, stop.symbol, stop.trigger):
            # Budget exhausted. Stay quiet here — stop_coverage already pages
            # about the uncovered position every tick, and repeating it from the
            # placement path only doubles the noise about one problem.
            _log.debug(
                "stop_place_budget_exhausted",
                account_ref=account_ref,
                symbol=stop.symbol,
                trigger=str(stop.trigger),
            )
            continue
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
            reset_place_failures(fail_key)
        except Exception:
            _place_failures[fail_key] = _place_failures.get(fail_key, 0) + 1
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

    if plan.place or plan.cancel or plan.attached:
        _log.info(
            "stop_protection_swept",
            account_ref=account_ref,
            placed=[s.symbol for s in plan.place],
            canceled=[o.symbol for o in plan.cancel],
            venue_attached=plan.attached,
        )
    return plan
