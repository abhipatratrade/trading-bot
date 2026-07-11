"""
NSE market-hours gate for equity buckets.

Crypto trades 24/7; Indian cash equity does not. This gate lets ``BucketRunner``
run the equity pipeline only when it can actually act:

    CLOSED          → do nothing (outside session / weekend / holiday)
    OPEN_NO_ENTRY   → manage exits only (session open, past the entry window)
    ENTRY_WINDOW    → full pipeline (new entries allowed)

Entries are confined to a short morning window (~09:45–10:30 IST) because the
strategy is a 09:45 gap-momentum entry — we must not open a position at 14:00 on
a gap that was measured at the open. Exits (Supertrend flip / 30-day cap) run any
time the session is open.

HOLIDAYS: the set below is deliberately conservative — it lists only
high-confidence fixed-date NSE holidays. Bias matters: an UNLISTED holiday just
means Dhan rejects the order (harmless), whereas an incorrectly-listed trading
day would make us skip real entries. Update ``NSE_HOLIDAYS`` from the official
NSE circular each year (movable festivals — Holi, Mahashivratri, Eid, Diwali,
etc. — must be added there).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum

IST = timezone(timedelta(hours=5, minutes=30))

# NSE cash-equity continuous session.
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
# Entry window: the strategy's 09:45 moment, with slack for tick cadence.
ENTRY_START = time(9, 45)
ENTRY_END = time(10, 30)

# Conservative fixed-date NSE trading holidays (see module note — extend yearly
# from the official circular; movable-festival dates are intentionally omitted).
NSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 26),   # Republic Day
        date(2026, 5, 1),    # Maharashtra Day
        date(2026, 8, 15),   # Independence Day
        date(2026, 10, 2),   # Gandhi Jayanti
        date(2026, 12, 25),  # Christmas
    }
)


class NseSession(StrEnum):
    CLOSED = "closed"
    OPEN_NO_ENTRY = "open_no_entry"
    ENTRY_WINDOW = "entry_window"


def is_trading_day(d: date) -> bool:
    """True on Mon–Fri that are not listed NSE holidays."""
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def nse_session(now: datetime) -> NseSession:
    """Classify ``now`` (any tz; naive treated as UTC) into an NSE session state."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    ist_now = now.astimezone(IST)
    if not is_trading_day(ist_now.date()):
        return NseSession.CLOSED
    t = ist_now.time()
    if not (SESSION_OPEN <= t <= SESSION_CLOSE):
        return NseSession.CLOSED
    if ENTRY_START <= t <= ENTRY_END:
        return NseSession.ENTRY_WINDOW
    return NseSession.OPEN_NO_ENTRY
