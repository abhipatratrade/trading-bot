"""Bar hygiene: never let a strategy see a bar that is still being written.

Dhan's charts endpoint returns the bar CURRENTLY FORMING alongside the
completed ones. ``DhanData._parse_candles`` does not drop it and neither does
``get_ohlcv_history``, so ``bars[-1]`` on a live feed is, most of the time, a
partial bar whose ``close`` is just the last traded price and which will keep
moving until its period elapses.

That is not a rounding detail. An indicator evaluated on a partial bar answers
a different question every time it is asked, so a strategy reading ``bars[-1]``
flips its mind repeatedly inside a single bar and trades on whichever answer it
happened to see. Observed live on 2026-09-04, commodity-indian::

    last COMPLETE bar 17:00        -> LONG
    + forming 17:15 bar @ 278.7    -> FLAT     <- the bot sold here, 278.70
    + forming 17:15 bar @ 278.0    -> LONG
    + forming 17:15 bar @ 277.2    -> LONG

The completed 17:15 bar leaves the machine LONG; the real exit was two bars
later at 17:45 and ~280.30, which is where the backtest and the TradingView
twin both put it. The bot did not disagree with the backtest — it answered
before the question had finished being asked.

swing-indian met the same bug first and solved it locally: ``mean_touched``
pins to ``last_complete_bar_key`` and truncates the frame through that bin,
after an unpinned read flipped 161 exit decisions over the 2026-07-27..31 week
(see ``src/shared/scanner/meanrev.py``). This module is that idea with the
NSE-specific bin arithmetic removed, so a strategy on any venue can ask the
plain question — has this bar's period actually elapsed? — without reimplementing
it and getting it wrong.

Pure. ``now`` is a parameter, never ``datetime.now()``, so the backtest and the
tests control it the same way the live path does.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, TypeVar


class _Timestamped(Protocol):
    """Any bar carrying the instant its period OPENED."""

    @property
    def timestamp(self) -> datetime: ...


BarT = TypeVar("BarT", bound=_Timestamped)


def completed_bars(
    bars: list[BarT], *, minutes: int, now: datetime
) -> list[BarT]:
    """``bars`` with any trailing still-forming bar removed.

    A bar stamped ``T`` covers ``[T, T + minutes)`` and is complete once ``now``
    has reached ``T + minutes``. Anything at or beyond that boundary is settled
    and kept; anything short of it is still being written and is dropped.

    Trailing only, and by iteration rather than a single test: the series is
    sorted, so a future-stamped bar can only sit at the end, but a feed that
    ever returns two of them must lose both. Dropping just the last would leave
    a partial bar in the exact position a caller reads as final, which is the
    failure this function exists to prevent.

    A boundary bar is KEPT (``>=``, not ``>``). At ``T + minutes`` exactly, the
    period has elapsed; excluding it would discard a good bar for a full cycle
    and, on a loop that ticks slower than the bar, sometimes forever.

    Returns a new list. An empty input, or an input that is entirely in the
    future, yields an empty list rather than raising — a caller that cannot act
    without bars already has to handle "not enough history", and this is that
    same case.
    """
    period = timedelta(minutes=minutes)
    out = list(bars)
    while out and out[-1].timestamp + period > now:
        out.pop()
    return out


__all__ = ["completed_bars"]
