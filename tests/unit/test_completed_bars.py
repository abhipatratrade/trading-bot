"""A strategy must never see the bar Dhan is still writing.

The live incident these pin down: on 2026-09-04 the commodity bucket read
``bars[-1]`` on a partial 15m bar, went FLAT on a transient value of it, and
sold at 278.70. The completed bar left the machine LONG; the real exit was two
bars later near 280.30. See ``src/shared/bars.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from src.shared.bars import completed_bars

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class _B:
    timestamp: datetime
    close: float = 0.0


def _series(*hhmm: str, day: str = "2026-09-04") -> list[_B]:
    """Bars stamped with the instant their period OPENED, IST."""
    return [
        _B(datetime.fromisoformat(f"{day}T{t}:00").replace(tzinfo=IST))
        for t in hhmm
    ]


def _at(hhmm: str, day: str = "2026-09-04") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=IST)


# ── the live case ───────────────────────────────────────────────────────


def test_the_forming_bar_is_dropped() -> None:
    """21:58 IST: the 21:45 bar has 2 minutes left to run and must not be
    replayed. This is verbatim what the live feed returned that evening."""
    bars = _series("21:15", "21:30", "21:45")
    kept = completed_bars(bars, minutes=15, now=_at("21:58"))
    assert [b.timestamp.strftime("%H:%M") for b in kept] == ["21:15", "21:30"]


def test_the_bar_is_kept_the_moment_its_period_elapses() -> None:
    """At exactly T+15 the bar is settled. Excluding it would throw away a
    good bar for a whole cycle — and forever on a loop slower than the bar."""
    bars = _series("21:15", "21:30", "21:45")
    kept = completed_bars(bars, minutes=15, now=_at("22:00"))
    assert len(kept) == 3


def test_a_settled_series_is_returned_unchanged() -> None:
    """Between sessions nothing is forming; the filter must be a no-op."""
    bars = _series("23:00", "23:15")
    assert completed_bars(bars, minutes=15, now=_at("23:59")) == bars


# ── the edges ───────────────────────────────────────────────────────────


def test_more_than_one_future_bar_is_removed() -> None:
    """Dropping only the last would leave a partial bar in the exact slot the
    caller reads as final — the failure this function exists to prevent."""
    bars = _series("21:15", "21:30", "21:45", "22:00")
    kept = completed_bars(bars, minutes=15, now=_at("21:50"))
    assert [b.timestamp.strftime("%H:%M") for b in kept] == ["21:15", "21:30"]


def test_an_all_future_series_yields_empty_not_an_error() -> None:
    """The caller already handles 'not enough history'; this is that case."""
    assert completed_bars(_series("22:00"), minutes=15, now=_at("21:00")) == []


def test_empty_input_is_empty_output() -> None:
    assert completed_bars([], minutes=15, now=_at("21:00")) == []


def test_the_input_list_is_not_mutated() -> None:
    """The caller may still want the raw feed for logging or diagnostics."""
    bars = _series("21:30", "21:45")
    completed_bars(bars, minutes=15, now=_at("21:50"))
    assert len(bars) == 2


def test_the_period_is_honoured_not_assumed() -> None:
    """A 60m bar stamped 21:00 is not complete at 21:58, though a 15m one
    stamped 21:45 would be at 22:00. Hard-coding 15 would pass the other
    tests and silently mis-handle every other timeframe."""
    hourly = [_B(_at("21:00"))]
    assert completed_bars(hourly, minutes=60, now=_at("21:58")) == []
    assert len(completed_bars(hourly, minutes=60, now=_at("22:00"))) == 1


def test_utc_and_ist_stamps_compare_correctly() -> None:
    """Dhan's epochs arrive as tz-aware UTC; ``now`` is UTC too. A naive
    comparison against IST wall-clock would be 5h30m wrong in the direction
    that keeps forming bars."""
    bar = _B(datetime(2026, 9, 4, 16, 15, tzinfo=UTC))  # 21:45 IST
    assert completed_bars([bar], minutes=15, now=datetime(2026, 9, 4, 16, 28, tzinfo=UTC)) == []
    assert len(
        completed_bars([bar], minutes=15, now=datetime(2026, 9, 4, 16, 30, tzinfo=UTC))
    ) == 1
