"""
Bot position ownership on SHARED broker accounts (Decision 027 → 030-followup).

Crypto buckets each trade an isolated Delta sub-account (Decision 019), so every
position on the account is the bot's — the safety sweeps can treat the whole
account as theirs. The Indian buckets instead SHARE one Dhan account with the
user's own manual trading (Decision 027). On 2026-07-22, minutes into going
live, that let the stop-protection sweep try to place stops on the user's NIFTY
option positions — it managed the account, not just what the bot opened.

This module answers the one question every account-level path needs on a shared
account: **which symbols, and how many units, has the BOT itself opened?** The
answer is derived only from the bot's own order flow (its ``Trade`` rows), never
from ``get_positions()`` — the exchange can't tell us who placed an order, but
our Trade rows can.

Indian CASH strategies are long-only (gap_down_reversal, mean_reversion_1h), so
a bot holding there is: filled/placed BUY entries minus executed SELL exits, and
a symbol with a net ≤ 0 is not the bot's.

Decision 036 breaks that assumption. The options bucket may open with a SELL, so
ownership is now computed SIGNED (``net_owned_signed``) and the long-only view
is derived from it. A caller that may hold shorts and uses the long-only view
will treat its own naked short as a stranger's position — no stop, no flatten,
no reconciliation. See ``net_owned_signed``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.logging import get_logger
from src.core.models import BrokerName, OrderSide, OrderStatus, Trade
from src.shared.bucket import load_bucket

_log = get_logger("order_manager.ownership")

# Widest bot holding period across the Indian buckets (swing-indian caps at
# ~30 days) plus a buffer. A bot entry older than this can't correspond to a
# position still open today, so a matching exchange position is the user's.
OWNERSHIP_WINDOW_DAYS = 40

# Entry BUYs count as owned the moment they're PLACED (not just filled) so the
# bot recognises its own just-opened position on the very next reconcile, before
# the fill status propagates. REJECTED/CANCELED never count.
_ENTRY_STATES = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.OPEN,
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
    }
)
# Exit SELLs only reduce ownership once they've actually executed — counting a
# not-yet-filled exit could make the bot think it no longer holds a position it
# still does, and then abandon (never stop/exit) it.
_EXIT_STATES = frozenset({OrderStatus.PARTIAL, OrderStatus.FILLED})


class _TradeLike(Protocol):
    """The fields ``net_owned`` reads off a trade (ORM Trade satisfies this)."""

    symbol: str
    side: OrderSide
    quantity: Decimal | None
    status: OrderStatus


def _closes_exposure(trade: _TradeLike) -> bool:
    """Does this order REDUCE the position, or open one? (Decision 036)

    The distinction drives which order states count, and getting it wrong is
    expensive in both directions: an opening order must count as soon as it is
    PLACED so the bot recognises its own position on the next sweep, while a
    closing order must count only once EXECUTED, because treating an unfilled
    close as done makes the bot abandon a position it still holds.

    ``reduce_only`` is exactly the flag for "this decreases exposure", and the
    OrderManager stamps it into ``Trade.extra`` on every exit, protective stop
    and breaker flatten. It is read from an attribute first (test fakes) and
    from ``extra`` second (the ORM row).

    When the flag is absent entirely — every trade written before it was
    stamped — this falls back to the LONG-ONLY rule a SELL closes. That
    fallback is what keeps the pre-036 behaviour byte-identical for historic
    rows rather than silently re-classifying them.
    """
    flag = getattr(trade, "reduce_only", None)
    if flag is None:
        extra = getattr(trade, "extra", None) or {}
        if isinstance(extra, dict) and "reduce_only" in extra:
            flag = extra["reduce_only"]
    if flag is None:
        return trade.side == OrderSide.SELL
    return bool(flag)


def net_owned_signed(trades: Iterable[_TradeLike]) -> dict[str, Decimal]:
    """``{symbol: signed_net_qty}`` — POSITIVE long, NEGATIVE short. PURE.

    Decision 036 added this, and the reason is a live-money hazard rather than
    tidiness. Every Indian strategy so far has been long-only, so ``net_owned``
    below drops anything with a net ≤ 0 as "not ours". The options bucket
    OPENS WITH A SELL. Under the long-only view a naked short position nets
    negative and is therefore invisible: the stop sweep skips it, the
    reconciler files it as the user's, and the breaker flatten passes over it.
    A short option with no stop, that no safety path believes it owns, is the
    worst position this system could hold.

    So ownership is computed signed, and the long-only view is derived FROM it
    rather than computed separately — two implementations of "what do we own"
    is precisely how ``_load_attribution`` and ``_load_stop_distances`` drifted
    apart and cost a live position its stop.

    Entry and exit states differ by DIRECTION, not by side, which is the
    subtlety a naive sign flip misses. Opening exposure counts as soon as it is
    PLACED, so the bot recognises its own position on the very next sweep;
    closing exposure counts only once EXECUTED, because treating an unfilled
    close as done would make the bot abandon a position it still holds.
    """
    net: dict[str, Decimal] = {}
    for t in trades:
        qty = t.quantity or Decimal("0")
        if qty <= 0:
            continue
        states = _EXIT_STATES if _closes_exposure(t) else _ENTRY_STATES
        if t.status not in states:
            continue
        signed = qty if t.side == OrderSide.BUY else -qty
        net[t.symbol] = net.get(t.symbol, Decimal("0")) + signed
    return {sym: q for sym, q in net.items() if q != 0}


def net_owned(trades: Iterable[_TradeLike]) -> dict[str, Decimal]:
    """``{symbol: net_long_qty}`` from a set of the bot's own trades. PURE.

    The LONG-ONLY view, and the one every pre-Decision-036 caller wants: the
    stop sweep, the reconciler and the breaker all predate short positions.
    Derived from :func:`net_owned_signed` so the two can never disagree.

    A caller that may hold shorts — anything touching the options bucket —
    must use the signed form instead, or it will treat its own short as a
    stranger's position.
    """
    return {sym: q for sym, q in net_owned_signed(trades).items() if q > 0}


@cache
def bucket_allows_shorts(bucket_id: str) -> bool:
    """Can this bucket legitimately hold a SHORT? Cached for the process.

    ``buckets.yaml`` is in git and only changes across a restart, and
    ``load_bucket`` re-reads and re-validates the whole file on every call —
    which this asks on the hot path, once per symbol-set per tick.

    An id that is not in ``buckets.yaml`` answers False: that means the bot is
    not running the bucket, so the rows are historical, and False is the
    pre-036 view they were written under. Never let this raise — it sits inside
    the ownership check that every safety sweep depends on.
    """
    try:
        return load_bucket(bucket_id).allows_shorts
    except Exception:
        _log.warning("ownership_bucket_unknown", bucket_id=bucket_id)
        return False


def bot_owned_quantities(
    session: Session,
    *,
    broker_name: BrokerName,
    bucket_ids: list[str],
    now: datetime,
    window_days: int = OWNERSHIP_WINDOW_DAYS,
    signed: bool | None = None,
) -> dict[str, Decimal]:
    """``{symbol: net_qty}`` the bot holds — the DB-backed wrapper.

    Thin: it windows + scopes the bot's Trade rows and defers the arithmetic to
    ``net_owned``. Only ever called for SHARED accounts; crypto keeps trusting
    ``get_positions()`` wholesale (its account is exclusively the bot's,
    Decision 019).

    ``signed`` (Decision 036) returns negative quantities for shorts. It must
    be True for any caller that can hold them: with the long-only default a
    naked short is absent from the result, which every safety path reads as
    "not ours" — no stop, no flatten, no reconciliation.

    **It defaults to the BUCKETS, not to False.** Shipped as ``signed: bool =
    False`` with the note that callers who need it would pass it, and the six
    that need it did not. ``BucketWatch.derivatives`` — the one flag that
    selected it — was never assigned at its only construction site, so the
    whole path was dead. commodity-indian opened a real short on 2026-09-04 and
    every account-level check filed it under the user's own trading: the
    reconciler refused to adopt it, so ``select_exits`` never ran; the sweep
    saw nothing to protect; ``foreign_positions`` reported it as a stranger's.

    So the question is now answered from the bucket that would hold the
    position rather than from each caller's memory. A cash-equity bucket has
    ``allows_shorts`` False and keeps exactly the pre-036 long-only view, which
    is what stops a settlement artifact (a sale out of holdings shows as a
    negative day-position) from being read as a real short. Pass ``signed``
    explicitly only to override that, and say why.
    """
    if signed is None:
        signed = any(bucket_allows_shorts(b) for b in bucket_ids)
    if not bucket_ids:
        return {}
    cutoff = now - timedelta(days=window_days)
    rows = (
        session.execute(
            select(Trade).where(
                Trade.broker == broker_name,
                Trade.bucket_id.in_(bucket_ids),
                Trade.created_at >= cutoff,
            )
        )
        .scalars()
        .all()
    )
    return net_owned_signed(rows) if signed else net_owned(rows)
