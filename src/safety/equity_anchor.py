"""
Start-of-day equity anchor for the daily-drawdown breaker (Decision 023).

One row per (sub-account, UTC day) in ``daily_equity_anchor``. The first
breaker pass of the day writes the row with that moment's equity; every
later pass — including after a bot restart — reads it back, so the anchor
survives crashes and the drawdown measure covers realized + unrealized
losses since the day began.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import DailyEquityAnchor

_log = get_logger("safety.equity_anchor")


def get_or_create_daily_anchor(
    account_ref: str,
    equity_now: Decimal,
    clock: Clock | None = None,
) -> Decimal:
    """Return today's (UTC) anchor equity, creating it if absent.

    Never raises on the unique-constraint race (two writers on the same
    day): the loser re-reads the winner's row.
    """
    today = (clock or RealClock()).now().date()

    with session_scope() as session:
        row = session.execute(
            select(DailyEquityAnchor).where(
                DailyEquityAnchor.account_ref == account_ref,
                DailyEquityAnchor.date == today,
            )
        ).scalar_one_or_none()
        if row is not None:
            return row.equity

    try:
        with session_scope() as session:
            session.add(
                DailyEquityAnchor(
                    account_ref=account_ref,
                    date=today,
                    equity=equity_now,
                )
            )
        _log.info(
            "daily_equity_anchor_created",
            account_ref=account_ref,
            date=str(today),
            equity=str(equity_now),
        )
        return equity_now
    except IntegrityError:
        with session_scope() as session:
            row = session.execute(
                select(DailyEquityAnchor).where(
                    DailyEquityAnchor.account_ref == account_ref,
                    DailyEquityAnchor.date == today,
                )
            ).scalar_one_or_none()
            return row.equity if row is not None else equity_now
