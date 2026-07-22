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

Indian strategies are long-only (gap_down_reversal, blasting_momentum), so a bot
holding is: filled/placed BUY entries minus executed SELL exits. A symbol with a
net ≤ 0 is not the bot's and must be left completely alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.models import BrokerName, OrderSide, OrderStatus, Trade

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


def net_owned(trades: Iterable[_TradeLike]) -> dict[str, Decimal]:
    """``{symbol: net_long_qty}`` from a set of the bot's own trades. PURE.

    BUY entries (placed → filled) add; SELL exits (executed) subtract. Only
    symbols with a strictly positive net remain. No DB, no clock — the caller
    supplies the already-windowed, already-scoped trades, which keeps this
    exhaustively unit-testable without a database.
    """
    net: dict[str, Decimal] = {}
    for t in trades:
        qty = t.quantity or Decimal("0")
        if t.side == OrderSide.BUY and t.status in _ENTRY_STATES:
            net[t.symbol] = net.get(t.symbol, Decimal("0")) + qty
        elif t.side == OrderSide.SELL and t.status in _EXIT_STATES:
            net[t.symbol] = net.get(t.symbol, Decimal("0")) - qty
    return {sym: q for sym, q in net.items() if q > 0}


def bot_owned_quantities(
    session: Session,
    *,
    broker_name: BrokerName,
    bucket_ids: list[str],
    now: datetime,
    window_days: int = OWNERSHIP_WINDOW_DAYS,
) -> dict[str, Decimal]:
    """``{symbol: net_long_qty}`` the bot holds long — the DB-backed wrapper.

    Thin: it windows + scopes the bot's Trade rows and defers the arithmetic to
    ``net_owned``. Only ever called for SHARED accounts; crypto keeps trusting
    ``get_positions()`` wholesale (its account is exclusively the bot's,
    Decision 019).
    """
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
    return net_owned(rows)
