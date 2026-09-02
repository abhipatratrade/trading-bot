"""An entry fires on the bar that OPENS a trade, not every tick it is held.

Until 2026-09-02 `select_entries` asked only whether the settled CCI machine
HELD a position — true on every tick for the whole life of the trade — so a buy
was proposed every 60 seconds from entry to exit. The only thing preventing a
duplicate was the sizer's dedup gate noticing the bot already held it, which
made the LEDGER the sole guard.

The ledger was then wrong twice in two days, for unrelated reasons, and both
times a second position was opened on ONE signal:

    09-01  a symbol mismatch hid the position; two lots fifteen minutes apart,
           both stamped signal_price 272.8
    09-02  a stop MCX stripped the trigger from liquidated the position and the
           machine re-entered at once; both stamped signal_price 280.0

A transition test needs no ledger, and matches the backtest — which took 125
trades, not one per tick.
"""

from __future__ import annotations

from decimal import Decimal

from src.shared.scanner.cci import Pos, Signal
from src.strategies.commodity.indian.strategies.cci_gas_reversion_15m import (
    CciGasReversion15m,
)

_LAST = "2026-09-02T17:00:00"
_EARLIER = "2026-09-02T16:45:00"


class _State:
    def __init__(self, pos: Pos = Pos.LONG) -> None:
        self.pos = pos
        self.entry_price = Decimal("280.0")
        self.stop_pct = Decimal("4.5")


class _Contract:
    symbol = "NATGASMINI-20260925-FUT"


def _enter(ts: str) -> Signal:
    return Signal(ts=ts, action="enter", side="buy", price=Decimal("280.0"),
                  reason="signal")


def _exit(ts: str) -> Signal:
    return Signal(ts=ts, action="exit", side="sell", price=Decimal("281.0"),
                  reason="signal")


def _strategy(signals: list[Signal], pos: Pos = Pos.LONG) -> CciGasReversion15m:
    s = CciGasReversion15m()
    s._state_for = lambda u, d: (_State(pos), _Contract(), signals, _LAST)  # type: ignore[assignment]
    return s


def test_fires_on_the_bar_that_opened_the_position() -> None:
    out = _strategy([_enter(_LAST)]).select_entries(["NATGASMINI"], object())
    assert len(out) == 1
    assert out[0].side == "buy"
    assert out[0].symbol == "NATGASMINI"
    assert out[0].hint["signal_price"] == "280.0"


def test_silent_while_merely_holding_the_position() -> None:
    """THE REGRESSION. The machine is still long, but this bar opened nothing —
    so there is no trade to place, whatever the ledger says."""
    out = _strategy([_enter(_EARLIER)]).select_entries(["NATGASMINI"], object())
    assert out == []


def test_silent_when_the_window_holds_no_entry_at_all() -> None:
    out = _strategy([]).select_entries(["NATGASMINI"], object())
    assert out == []


def test_an_exit_on_the_closing_bar_is_not_an_entry() -> None:
    """`step` can emit an exit and an entry on one bar, so the test must ask
    for an `enter` specifically rather than for any signal."""
    out = _strategy([_exit(_LAST)]).select_entries(["NATGASMINI"], object())
    assert out == []


def test_an_exit_that_frees_an_entry_still_fires() -> None:
    """The genuine two-signal bar: closing one trade and opening the next."""
    out = _strategy([_exit(_LAST), _enter(_LAST)]).select_entries(
        ["NATGASMINI"], object()
    )
    assert len(out) == 1


def test_a_flat_machine_never_enters() -> None:
    out = _strategy([_enter(_LAST)], pos=Pos.FLAT).select_entries(
        ["NATGASMINI"], object()
    )
    assert out == []
