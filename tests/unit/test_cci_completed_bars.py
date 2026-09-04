"""The CCI strategy replays settled bars only.

``tests/unit/test_cci_entry_transition.py`` stubs ``_state_for`` outright, so
nothing there touches the feed. These go through the real one.

The live failure, 2026-09-04 on commodity-indian: Dhan returns the bar it is
still writing, ``_state_for`` replayed it, and the machine answered on a close
that had not settled::

    last COMPLETE bar 17:00        -> LONG
    + forming 17:15 bar @ 278.7    -> FLAT     <- sold here, 278.70
    + forming 17:15 bar @ 277.2    -> LONG

The completed 17:15 bar leaves it LONG. The real exit came at 17:45 near
280.30, where the backtest and the Pine twin both put it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import src.strategies.commodity.indian.strategies.cci_gas_reversion_15m as mod
from src.shared.scanner.cci import Bar, CCIState
from src.strategies.commodity.indian.strategies.cci_gas_reversion_15m import (
    CciGasReversion15m,
)

_OPEN = datetime(2026, 9, 4, 3, 30, tzinfo=UTC)  # 09:00 IST, MCX open


@dataclass(frozen=True)
class _Bar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class _Contract:
    symbol = "NATGASMINI-20260925-FUT"


class _Data:
    """Only what ``_state_for`` reaches for."""

    def __init__(self, bars: list[_Bar]) -> None:
        self._bars = bars
        self.asked: list[tuple[str, str, int]] = []

    def get_ohlcv_history(self, symbol: str, tf: str, *, days: int) -> list[_Bar]:
        self.asked.append((symbol, tf, days))
        return list(self._bars)


def _series(n: int, *, spike_last: bool = False) -> list[_Bar]:
    """``n`` 15m bars walking gently, optionally ending on an outlier.

    The spike stands in for a partial bar's transient close: a value that is
    real for a moment and gone by the time the period elapses.
    """
    out: list[_Bar] = []
    for i in range(n):
        px = Decimal("280") + Decimal(i % 7) / 10
        if spike_last and i == n - 1:
            px = Decimal("340")
        out.append(
            _Bar(
                timestamp=_OPEN + timedelta(minutes=15 * i),
                open=px,
                high=px + Decimal("0.5"),
                low=px - Decimal("0.5"),
                close=px,
            )
        )
    return out


def _strategy(data: _Data, monkeypatch, *, now: datetime) -> CciGasReversion15m:
    s = CciGasReversion15m()
    s._contract_for = lambda u, d: _Contract()  # type: ignore[assignment]

    class _Frozen:
        def now(self) -> datetime:
            return now

    monkeypatch.setattr(mod, "RealClock", _Frozen)
    return s


def _replay(bars: list[_Bar]) -> CCIState:
    st = CCIState()
    st.run([Bar(ts=b.timestamp, open=b.open, high=b.high, low=b.low,
                close=b.close) for b in bars])
    return st


# ── the filter is actually applied ──────────────────────────────────────


def test_last_ts_names_the_settled_bar_not_the_forming_one(monkeypatch) -> None:
    """``last_ts`` is what ``select_entries`` compares a signal against. If it
    named the forming bar the gate could only fire while that bar moved."""
    bars = _series(60)
    data = _Data(bars)
    # Two minutes into the final bar — it has thirteen left to run.
    now = bars[-1].timestamp + timedelta(minutes=2)
    got = _strategy(data, monkeypatch, now=now)._state_for("NATGASMINI", data)

    assert got is not None
    assert got[3] == bars[-2].timestamp, "replayed the bar still being written"


def test_a_settled_series_keeps_its_final_bar(monkeypatch) -> None:
    """Between sessions nothing is forming; the filter must not eat a bar."""
    bars = _series(60)
    data = _Data(bars)
    now = bars[-1].timestamp + timedelta(minutes=15)
    got = _strategy(data, monkeypatch, now=now)._state_for("NATGASMINI", data)

    assert got is not None
    assert got[3] == bars[-1].timestamp


def test_the_machine_matches_a_completed_only_replay(monkeypatch) -> None:
    """Equivalence, not just the timestamp: the state the strategy returns is
    the one you get by replaying the settled bars and nothing else."""
    bars = _series(60, spike_last=True)
    data = _Data(bars)
    now = bars[-1].timestamp + timedelta(minutes=2)
    got = _strategy(data, monkeypatch, now=now)._state_for("NATGASMINI", data)

    assert got is not None
    settled = _replay(bars[:-1])
    assert got[0].pos == settled.pos
    assert got[0].entry_price == settled.entry_price


def test_the_history_floor_counts_settled_bars(monkeypatch) -> None:
    """40 bars of which one is still forming is 39 bars of history. Counting
    the partial one would let the machine start one bar early, on a series it
    has already been told is too short to trust."""
    bars = _series(40)
    data = _Data(bars)
    now = bars[-1].timestamp + timedelta(minutes=2)

    assert _strategy(data, monkeypatch, now=now)._state_for("NATGASMINI", data) is None
    # The same 40 bars, all settled, are enough.
    later = bars[-1].timestamp + timedelta(minutes=15)
    assert _strategy(data, monkeypatch, now=later)._state_for("NATGASMINI", data) is not None
