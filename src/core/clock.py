"""
Injectable clock.

Why bother: tests, replays, and the (separately built) backtest engine all
need to control "now". Hard-coding `datetime.now()` in business logic makes
those impossible. Every module that needs the current time imports a
``Clock`` and calls ``clock.now()``.

Production code uses ``RealClock``. Tests use ``FakeClock``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Return the current UTC datetime (timezone-aware)."""


class RealClock(Clock):
    """Wall-clock implementation. Always UTC, always tz-aware."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FakeClock(Clock):
    """Test/replay clock. Advance manually with ``tick()`` or ``set()``."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a tz-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def tick(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("FakeClock.set requires a tz-aware datetime")
        self._now = when.astimezone(UTC)


# Module-level default. Production entrypoints construct one and pass it
# down explicitly; this default exists for convenience in scripts.
real_clock = RealClock()
