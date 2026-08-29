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

# MCX commodity session (Decision 037). Runs ~8 hours longer than NSE's, which
# is why the session gate had to become per-EXCHANGE rather than stay a pair of
# module constants: a commodity bucket checked against SESSION_CLOSE would go
# dark at 15:30 and miss the 18:00 IST NYMEX open, where 28 of the CCI gas
# strategy's 125 trades entered.
#
# The close is 23:30 for most of the year and 23:55 while US daylight saving is
# in force. 23:30 is used unconditionally: it is the SHORTER window, so the
# only cost is not trading a 25-minute tail, whereas assuming the longer one
# would have the bot expect bars that do not exist for half the year.
MCX_SESSION_OPEN = time(9, 0)
MCX_SESSION_CLOSE = time(23, 30)

# Session bounds per exchange, keyed by ``BucketConfig.exchange``.
SESSION_HOURS: dict[str, tuple[time, time]] = {
    "NSE": (SESSION_OPEN, SESSION_CLOSE),
    "BSE": (SESSION_OPEN, SESSION_CLOSE),
    "MCX": (MCX_SESSION_OPEN, MCX_SESSION_CLOSE),
}

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


def nse_session(
    now: datetime,
    entry_start: time = ENTRY_START,
    entry_end: time = ENTRY_END,
    exchange: str = "NSE",
) -> NseSession:
    """Classify ``now`` (any tz; naive treated as UTC) into an NSE session state.

    The entry window is per-bucket: swing-indian keeps the 09:45 default above,
    while intraday-indian opens at 09:30 (its reversal candle can print as
    early as the 09:30 close — Decision 029). Both are declared in
    ``buckets.yaml`` and passed in by ``BucketRunner``.

    ``exchange`` selects the SESSION bounds (Decision 037). MCX trades
    09:00–23:30 against NSE's 09:15–15:30; a commodity bucket left on the NSE
    bounds would go dark at 15:30 and never see the 18:00 IST NYMEX open. The
    holiday calendar is deliberately shared — MCX observes the same national
    holidays this list carries, and a commodity-specific list would be a second
    thing to maintain for no difference on those dates.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    ist_now = now.astimezone(IST)
    if not is_trading_day(ist_now.date()):
        return NseSession.CLOSED
    t = ist_now.time()
    open_t, close_t = SESSION_HOURS.get(exchange, (SESSION_OPEN, SESSION_CLOSE))
    if not (open_t <= t <= close_t):
        return NseSession.CLOSED
    if entry_start <= t <= entry_end:
        return NseSession.ENTRY_WINDOW
    return NseSession.OPEN_NO_ENTRY


def parse_ist_time(value: str) -> time:
    """Parse a ``"HH:MM"`` string from YAML into a ``time``."""
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm))
