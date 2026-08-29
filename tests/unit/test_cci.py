"""CCI reversion state machine (Decision 037, Phase F).

The parity harness proves this module against all 125 backtested trades. These
tests pin the individual rules it depends on, so a regression names itself
instead of showing up as a parity percentage.

``test_a_stop_exit_bar_still_arms`` exists because the first port got it wrong:
it returned on the stop path before the arming test ran, and reproduced 122 of
125 trades. All three misses were that one rule.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.shared.scanner.cci import (
    DEFAULT_LENGTH,
    Bar,
    CCIState,
    Pos,
    cci_series,
)


def _bar(close: str, high: str | None = None, low: str | None = None,
         open_: str | None = None, ts: int = 0) -> Bar:
    c = Decimal(close)
    return Bar(
        ts=ts,
        open=Decimal(open_) if open_ else c,
        high=Decimal(high) if high else c,
        low=Decimal(low) if low else c,
        close=c,
    )


def _flat(n: int, price: str = "100") -> list[Bar]:
    return [_bar(price, ts=i) for i in range(n)]


# ── the indicator ───────────────────────────────────────────────────────
def test_warm_up_is_length_minus_one_bars() -> None:
    got = cci_series([_bar(str(100 + i), ts=i) for i in range(30)])
    assert got[: DEFAULT_LENGTH - 1] == [None] * (DEFAULT_LENGTH - 1)
    assert got[DEFAULT_LENGTH - 1] is not None


def test_mad_is_mean_absolute_deviation_not_stdev() -> None:
    """The usual porting error. On this window the two differ, so a stdev
    implementation cannot produce the same number."""
    bars = [_bar(str(v), ts=i) for i, v in enumerate([1, 2, 3, 4, 100])]
    got = cci_series(bars, length=5)[-1]
    tps = [Decimal(v) for v in (1, 2, 3, 4, 100)]
    sma = sum(tps) / 5
    mad = sum(abs(t - sma) for t in tps) / 5
    assert got == (tps[-1] - sma) / (Decimal("0.015") * mad)
    # And the stdev reading would be a materially different value.
    var = sum((t - sma) ** 2 for t in tps) / 5
    stdev = var.sqrt()
    assert abs(stdev - mad) > Decimal("5")


def test_a_flat_window_yields_no_signal_rather_than_dividing_by_zero() -> None:
    assert cci_series(_flat(25), length=DEFAULT_LENGTH)[-1] is None


def test_hlc3_is_the_source() -> None:
    b = _bar("100", high="110", low="90")
    assert b.hlc3 == Decimal("100")


# ── the state machine ───────────────────────────────────────────────────
def test_a_bar_arms_or_fires_never_both() -> None:
    """Reversing the order enters one bar early on every trade."""
    st = CCIState()
    # Deeply oversold: arms long, must NOT also fire on the same bar.
    st.step(_bar("100"), Decimal("-300"))
    assert st.armed_long and st.pos is Pos.FLAT
    # Back inside the band on a LATER bar: now it fires.
    out = st.step(_bar("101"), Decimal("-100"))
    assert st.pos is Pos.LONG
    assert [(s.action, s.side) for s in out] == [("enter", "buy")]


def test_short_side_is_the_mirror() -> None:
    st = CCIState()
    st.step(_bar("100"), Decimal("300"))
    assert st.armed_short
    st.step(_bar("99"), Decimal("100"))
    assert st.pos is Pos.SHORT


def test_arming_never_expires() -> None:
    """A setup armed 200 bars ago is still live — which is why the live
    replay window has to be long."""
    st = CCIState()
    st.step(_bar("100"), Decimal("-300"))
    for i in range(200):
        st.step(_bar("100", ts=i), Decimal("-240"))  # still outside the band
    assert st.armed_long
    st.step(_bar("100"), Decimal("-100"))
    assert st.pos is Pos.LONG


def test_entry_clears_the_armed_state() -> None:
    st = CCIState()
    st.step(_bar("100"), Decimal("-300"))
    st.step(_bar("100"), Decimal("-100"))
    assert st.pos is Pos.LONG
    assert not st.armed_long and not st.armed_short


# ── exits ───────────────────────────────────────────────────────────────
def _long_at(price: str) -> CCIState:
    st = CCIState()
    st.step(_bar(price), Decimal("-300"))
    st.step(_bar(price), Decimal("-100"))
    assert st.pos is Pos.LONG
    return st


def test_stop_sits_below_entry_for_a_long() -> None:
    st = _long_at("100")
    assert st.stop_price == Decimal("100") * (Decimal("1") - Decimal("0.045"))


def test_stop_fires_intrabar_on_the_low() -> None:
    st = _long_at("100")
    out = st.step(_bar("99", low="95"), Decimal("-50"))
    assert [s.reason for s in out] == ["stop"]
    assert out[0].price == Decimal("95.5")  # the stop level, not the close
    assert st.pos is Pos.FLAT


def test_a_bar_that_opened_beyond_the_stop_fills_at_the_open() -> None:
    """4 of the run's 125 trades exited this way; pretending they filled at
    the stop level would flatter every one of them."""
    st = _long_at("100")
    out = st.step(_bar("90", high="91", low="89", open_="90"), Decimal("-50"))
    assert out[0].reason == "stop_gap"
    assert out[0].price == Decimal("90")  # the open, below the 95.5 stop


def test_a_stop_exit_bar_still_arms() -> None:
    """THE BUG THE PARITY HARNESS CAUGHT. The first port returned on the stop
    path before the arming test ran and lost 3 of 125 trades — each one a stop
    firing on a bar whose CCI had also gone beyond the arm level."""
    st = _long_at("100")
    # open ABOVE the 95.5 stop so this is a plain stop, not a gap through it.
    out = st.step(_bar("94", low="90", open_="96"), Decimal("-300"))
    assert out[0].reason == "stop"
    assert st.pos is Pos.FLAT
    assert st.armed_long, "the stop bar's own excursion must arm the next entry"
    # And the entry does fire on a later bar.
    st.step(_bar("95"), Decimal("-100"))
    assert st.pos is Pos.LONG


def test_signal_exit_goes_flat_and_does_not_reverse_on_the_same_bar() -> None:
    """The delayed reversal is what separates this configuration from a
    stop-and-reverse one."""
    st = _long_at("100")
    out = st.step(_bar("110"), Decimal("300"))
    assert [(s.action, s.reason) for s in out] == [("exit", "signal")]
    assert st.pos is Pos.FLAT
    # +300 is beyond the +225 arm level, so the opposite side is now armed...
    assert st.armed_short
    # ...and fires only once CCI comes back inside.
    st.step(_bar("108"), Decimal("100"))
    assert st.pos is Pos.SHORT


def test_an_exit_clears_arms_on_both_sides() -> None:
    """Omitting this reset is the single most damaging porting bug available:
    the system re-enters immediately and churns the edge away."""
    st = CCIState()
    st.step(_bar("100"), Decimal("-300"))   # armed long
    st.step(_bar("100"), Decimal("-100"))   # entered long
    st.step(_bar("110"), Decimal("300"))    # signal exit
    assert not st.armed_long                 # the long arm is gone
    assert st.armed_short                    # only this bar's excursion remains


def test_stop_is_checked_before_the_signal_exit() -> None:
    """Priority order matters: a bar can satisfy both, and the stop wins."""
    st = _long_at("100")
    out = st.step(_bar("110", low="90"), Decimal("300"))
    assert out[0].reason == "stop"


def test_warm_up_bars_produce_no_signal() -> None:
    st = CCIState()
    assert st.step(_bar("100"), None) == []
    assert st.pos is Pos.FLAT and not st.armed_long


@pytest.mark.parametrize("side", ["long", "short"])
def test_a_replay_is_deterministic(side: str) -> None:
    """The live runner recomputes armed flags by replaying history rather than
    persisting them, so two replays of the same bars must agree exactly."""
    sign = Decimal("-1") if side == "long" else Decimal("1")
    bars = [_bar(str(100 + (i % 7)), ts=i) for i in range(60)]
    ccis = [sign * Decimal("300") if i % 11 == 0 else sign * Decimal("50")
            for i in range(60)]

    def _replay() -> list[tuple]:
        st = CCIState()
        seen = []
        for b, c in zip(bars, ccis, strict=True):
            for s in st.step(b, c):
                seen.append((s.ts, s.action, s.side, s.reason))
        return seen

    assert _replay() == _replay()
