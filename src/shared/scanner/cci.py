"""
CCI reversion state machine — MCX commodity futures (Decision 037, Phase F).

Port of the Backtesting Engine's `cci_reversion_mcx` (`cci_mcx.py`, `_CCIState`;
Pine twin `strategies/pine/cci_reversion_mcx_opt.pine`), validated by
``scripts/cci_gas_parity.py`` against the 125 trades in that run's handoff.

PURE. No DB, no broker, no clock, no I/O. The whole point is that the live path
and the parity harness execute the same code over the same bars, so "the port
matches the backtest" is a statement about this module rather than a hope.

Two details are where a reimplementation of this strategy goes wrong, and both
are called out in the handoff's RULES.md as the known traps:

1.  **``mad`` is the mean ABSOLUTE deviation, not the standard deviation.**
    Substituting stdev changes every CCI value and therefore every signal.

2.  **A bar may ARM or FIRE, never both.** The firing test runs FIRST; only if
    it fails does the arming test run. Reversing that order enters one bar
    early on every trade.

A third is subtler and costs the most: **after any exit, the armed state is
cleared on BOTH sides.** Without it the system re-enters immediately on the
next bar and the same configuration goes from +Rs 96,437 to roughly
break-even through pure churn.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum

# The validated configuration (RULES.md §6). Chosen for consistency across
# fill models and periods, NOT for the best backtest — ranking on in-sample
# profit is what produced every false positive in that study.
DEFAULT_LENGTH = 20
DEFAULT_ARM_LEVEL = Decimal("225")
DEFAULT_EXIT_LEVEL = Decimal("250")
DEFAULT_STOP_PCT = Decimal("4.5")

# CCI's conventional scaling constant, so that ~70-80% of values fall within
# +/-100. Part of the indicator's definition, not a tunable.
_CCI_SCALE = Decimal("0.015")


class Pos(IntEnum):
    SHORT = -1
    FLAT = 0
    LONG = 1


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed OHLC bar. ``ts`` is opaque here — any orderable value."""

    ts: object
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @property
    def hlc3(self) -> Decimal:
        return (self.high + self.low + self.close) / 3


@dataclass(frozen=True, slots=True)
class Signal:
    """One state transition worth acting on."""

    ts: object
    action: str          # "enter" | "exit"
    side: str            # "buy" | "sell" — the ORDER side
    price: Decimal       # the fill reference: bar close, or the stop level
    reason: str          # "signal" | "stop" | "stop_gap"
    cci: Decimal | None = None


def cci_series(bars: Sequence[Bar], length: int = DEFAULT_LENGTH) -> list[Decimal | None]:
    """CCI(length) on hlc3, TradingView's definition. PURE.

        tp   = (high + low + close) / 3
        sma  = SMA(tp, length)
        mad  = mean(|tp - sma|) over the same window
        cci  = (tp - sma) / (0.015 * mad)

    ``mad`` is the mean ABSOLUTE deviation about the window's mean — the usual
    porting error is to reach for the standard deviation, which is a different
    number and shifts every signal.

    The first ``length - 1`` values are None (warm-up); a window whose mad is
    zero — a perfectly flat stretch — also yields None rather than dividing by
    zero, which is the honest reading of "no dispersion, no signal".
    """
    out: list[Decimal | None] = []
    tps = [b.hlc3 for b in bars]
    n = length
    for i in range(len(tps)):
        if i + 1 < n:
            out.append(None)
            continue
        window = tps[i + 1 - n : i + 1]
        sma = sum(window) / n
        mad = sum(abs(tp - sma) for tp in window) / n
        if mad == 0:
            out.append(None)
            continue
        out.append((tps[i] - sma) / (_CCI_SCALE * mad))
    return out


@dataclass(slots=True)
class CCIState:
    """The state machine. Feed it bars in order; it yields signals.

    Every field here must survive a process restart. The live runner does NOT
    persist them — it replays recent history through this same class, which is
    deterministic given the bars. Replay a few hundred bars at least: arming
    never expires, so a setup armed long ago is still valid and a short replay
    window silently drops it.
    """

    length: int = DEFAULT_LENGTH
    arm_level: Decimal = DEFAULT_ARM_LEVEL
    exit_level: Decimal = DEFAULT_EXIT_LEVEL
    stop_pct: Decimal = DEFAULT_STOP_PCT

    pos: Pos = Pos.FLAT
    entry_price: Decimal | None = None
    armed_long: bool = False
    armed_short: bool = False
    _cci: list[Decimal | None] = field(default_factory=list, repr=False)

    @property
    def stop_price(self) -> Decimal | None:
        """The protective level for the open position, or None when flat."""
        if self.entry_price is None or self.pos is Pos.FLAT:
            return None
        frac = self.stop_pct / Decimal("100")
        if self.pos is Pos.LONG:
            return self.entry_price * (Decimal("1") - frac)
        return self.entry_price * (Decimal("1") + frac)

    def _clear_arms(self) -> None:
        """After ANY exit, both sides disarm.

        Re-entry then needs a genuinely fresh excursion beyond the arm level.
        Omitting this is the single most damaging porting bug available here.
        """
        self.armed_long = False
        self.armed_short = False

    def run(self, bars: Sequence[Bar]) -> list[Signal]:
        """Replay a whole series from the current state. PURE w.r.t. inputs."""
        self._cci = cci_series(bars, self.length)
        signals: list[Signal] = []
        for i, bar in enumerate(bars):
            signals.extend(self.step(bar, self._cci[i]))
        return signals

    def step(self, bar: Bar, cci: Decimal | None) -> list[Signal]:
        """Advance one bar. Returns 0-2 signals (an exit may free an entry).

        Order of evaluation is load-bearing and matches the backtest:
          1. the protective stop, which fires INTRABAR on the low/high;
          2. the signal exit, on the close;
          3. the firing test for a new entry, on the close;
          4. only then, the arming test.
        """
        out: list[Signal] = []

        # 1. Stop — intrabar, checked before anything reads the close.
        if self.pos is not Pos.FLAT:
            stop = self.stop_price
            assert stop is not None
            hit = (
                bar.low <= stop if self.pos is Pos.LONG else bar.high >= stop
            )
            if hit:
                # A bar that OPENED beyond the stop fills at the open, not at
                # the stop: the level was already gone when the session began.
                # 4 of the run's 125 trades exited this way, and pretending
                # they filled at the stop would flatter every one of them.
                gapped = (
                    bar.open < stop if self.pos is Pos.LONG else bar.open > stop
                )
                out.append(
                    Signal(
                        ts=bar.ts,
                        action="exit",
                        side="sell" if self.pos is Pos.LONG else "buy",
                        price=bar.open if gapped else stop,
                        reason="stop_gap" if gapped else "stop",
                        cci=cci,
                    )
                )
                self.pos = Pos.FLAT
                self.entry_price = None
                self._clear_arms()
                # A STOP-EXIT BAR STILL ARMS, exactly as a signal-exit bar does.
                #
                # Caught by the parity harness, not by reading: the first port
                # returned here and reproduced 122 of 125 trades. All three
                # misses were the same shape — a stop fired on a bar whose CCI
                # had also travelled beyond the arm level, and skipping the
                # arming test meant the setup was never registered, so the
                # entry one bar later never fired either.
                #
                # Clear first, then arm: the exit resets both sides, and only
                # then does THIS bar's excursion count.
                if cci is not None:
                    self._arm(cci)
                return out

        if cci is None:
            return out  # warm-up, or a flat window: no signal is computable

        # 2. Signal exit, on the close. Goes FLAT; it does not reverse here.
        if self.pos is Pos.LONG and cci >= self.exit_level:
            out.append(
                Signal(bar.ts, "exit", "sell", bar.close, "signal", cci)
            )
            self.pos = Pos.FLAT
            self.entry_price = None
            self._clear_arms()
        elif self.pos is Pos.SHORT and cci <= -self.exit_level:
            out.append(
                Signal(bar.ts, "exit", "buy", bar.close, "signal", cci)
            )
            self.pos = Pos.FLAT
            self.entry_price = None
            self._clear_arms()

        # The exit bar can ARM the opposite side (the exit level is beyond the
        # arm level), but it can never FIRE one — the delayed reversal is what
        # separates this configuration from a stop-and-reverse.
        if out:
            self._arm(cci)
            return out

        if self.pos is not Pos.FLAT:
            return out

        # 3. FIRE first...
        if self.armed_long and cci >= -self.arm_level:
            out.append(Signal(bar.ts, "enter", "buy", bar.close, "signal", cci))
            self.pos = Pos.LONG
            self.entry_price = bar.close
            self._clear_arms()
            return out
        if self.armed_short and cci <= self.arm_level:
            out.append(Signal(bar.ts, "enter", "sell", bar.close, "signal", cci))
            self.pos = Pos.SHORT
            self.entry_price = bar.close
            self._clear_arms()
            return out

        # 4. ...and only then ARM. A bar does one or the other, never both.
        self._arm(cci)
        return out

    def _arm(self, cci: Decimal) -> None:
        if cci < -self.arm_level:
            self.armed_long = True
        elif cci > self.arm_level:
            self.armed_short = True
