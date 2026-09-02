"""
CCI(20) 225/250 reversion on MCX natural gas mini, 15m (Decision 037).

backtest_ref: Backtesting Engine/results/handoff/cci_gas_15m/
  (handoff.yaml + RULES.md + trades.json; reference implementation `cci_mcx.py`,
   Pine twin `strategies/pine/cci_reversion_mcx_opt.pine`)

Parity: ``scripts/cci_gas_parity.py`` reproduces **125 of 125** backtested
trades through ``src.shared.scanner.cci`` — the same module this strategy calls.

READ BEFORE ENABLING THIS BUCKET. The handoff is explicit about three things
this code cannot fix, and they are the reason it ships disabled:

* **No out-of-sample fold, structurally.** TradingView caps intraday history, so
  15m gas starts 2025-12-01 and the whole sample IS the backtest. The same
  configuration is NEGATIVE on 30m and 1H over those months, and negative in 3
  of 4 years on 1H.
* **The edge is the unprotected exposure.** 81 of 125 trades are held across a
  session close and net +Rs 122,021; the 44 intraday-only trades net -Rs 25,584.
  MCX gas is shut 23:30-09:00 and over weekends, and the exchange-resident stop
  CANNOT FIRE while it is shut. Four trades gapped through it in-sample, on a
  series containing +16.0% and -10.8% session gaps against a 4.5% stop.
* **The roll is not in the backtest.** It traded a continuous front-month series
  with no expiry logic at all. The live 15-day floor (user instruction,
  2026-08-29) means roughly half of each month is spent on the NEXT contract —
  a different instrument at a different absolute price with thinner liquidity —
  and each roll pays a spread the spliced series never charged.

Signal source is the CONTRACT's own series, not a spot index: CCI is computed on
the futures prints. The strategy therefore resolves its own contract, using the
SAME selector and config the runner uses for execution, so the two cannot pick
different months.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from src.core.logging import get_logger
from src.core.models import Position
from src.data_sources.base import MarketData
from src.shared.base_strategy import EntryCandidate, Strategy
from src.shared.bucket import load_bucket
from src.shared.contract_selection import (
    ContractSelectionConfig,
    ContractSelector,
    load_contract_selection,
)
from src.shared.contracts import underlying_of
from src.shared.scanner.cci import Bar, CCIState, Pos, cci_series

_log = get_logger("strategies.cci_gas_reversion_15m")

# How much history to replay on every pass.
#
# The armed flags are NOT persisted — they are recomputed by replaying this
# window, which is deterministic given the bars (RULES.md §4). The window has to
# be long because ARMING NEVER EXPIRES: a setup armed weeks ago is still live,
# and a short replay silently drops it, which shows up as a missing trade rather
# than as an error. 90 days is Dhan's own intraday request ceiling, so this asks
# for the most it can get in one call.
_REPLAY_DAYS = 90

# Dhan's interval key for the 15-minute bar this strategy signals on.
_TF = "15m"


class CciGasReversion15m(Strategy):
    """Two-stage CCI reversion. Long and short, one position at a time."""

    name: ClassVar[str] = "cci_gas_reversion_15m"
    tf: ClassVar[str] = _TF
    trading_type: ClassVar[str] = "commodity"

    def __init__(self, bucket_id: str = "commodity-indian") -> None:
        self._bucket_id = bucket_id
        self._config: ContractSelectionConfig | None = None

    # ── contract resolution ─────────────────────────────────────────────
    def _selection_config(self) -> ContractSelectionConfig:
        if self._config is None:
            bucket = load_bucket(self._bucket_id)
            self._config = load_contract_selection(bucket.contracts_yaml_path)
        return self._config

    def _contract_for(self, underlying: str, data: MarketData):
        """The contract this strategy signals on — the runner's choice too.

        Both sides call the same selector with the same config on the same
        date, so they agree by construction rather than by coincidence. If they
        could disagree, the strategy would compute CCI on one month and the
        order would go to another.
        """
        registry = getattr(data, "fno", None)
        if registry is None:
            return None
        from src.core.clock import RealClock

        selector = ContractSelector(registry, self._selection_config())
        chosen = selector.select(
            underlying, spot=Decimal("1"), side="buy", on=RealClock().now().date()
        )
        return chosen.contract if chosen.ok else None

    def _state_for(
        self, underlying: str, data: MarketData
    ) -> tuple[CCIState, object, list, object] | None:
        """Replay the contract's bars and return the settled state machine."""
        contract = self._contract_for(underlying, data)
        if contract is None:
            _log.warning("cci_no_contract", underlying=underlying)
            return None
        try:
            raw = data.get_ohlcv_history(contract.symbol, _TF, days=_REPLAY_DAYS)
        except Exception:
            _log.warning(
                "cci_bars_unavailable", contract=contract.symbol, exc_info=True
            )
            return None
        if len(raw) < 40:
            _log.warning(
                "cci_insufficient_history",
                contract=contract.symbol,
                bars=len(raw),
            )
            return None

        bars = [
            Bar(ts=b.timestamp, open=b.open, high=b.high, low=b.low, close=b.close)
            for b in raw
        ]
        state = CCIState()
        # Keep the SIGNALS, not just the settled state. "Is this machine in a
        # position?" and "did this bar OPEN one?" are different questions, and
        # only the second one may place an order — see select_entries.
        signals = state.run(bars)
        return state, contract, signals, bars[-1].ts

    # ── entries ─────────────────────────────────────────────────────────
    def select_entries(
        self, candidates: list[str], data: MarketData
    ) -> list[EntryCandidate]:
        """Fire only on the bar that actually OPENED the position.

        The distinction is not pedantic; it is what stops the same setup being
        entered twice. Until 2026-09-02 this asked only whether the settled
        machine HELD a position, which is true on every tick for the whole life
        of the trade — so a buy was proposed every 60 seconds from entry to
        exit, and the only thing preventing a duplicate was the sizer's dedup
        gate noticing the bot already held it.

        That made the ledger the sole guard, and the ledger has been wrong
        twice for unrelated reasons: on 09-01 a symbol mismatch hid the position
        (two lots opened fifteen minutes apart on ONE signal, both stamped
        signal_price 272.8), and on 09-02 a stop that MCX stripped the trigger
        from liquidated it and the machine re-entered immediately (both stamped
        280.0). Same mechanism, same day-shape, different root cause.

        A transition test needs no ledger. If the last bar produced an ``enter``
        signal, this is a new trade; otherwise the machine is merely still in
        one, and there is nothing to place. The backtest took 125 trades, not
        one per tick, so this is also the reading that matches it — dedup
        becomes a second line of defence rather than the only one.
        """
        out: list[EntryCandidate] = []
        for underlying in candidates:
            got = self._state_for(underlying, data)
            if got is None:
                continue
            state, contract, signals, last_ts = got
            if state.pos is Pos.FLAT or state.entry_price is None:
                continue
            # THE TRANSITION TEST. `step` can return an exit and an entry on the
            # same bar (an exit frees an entry), so this asks for an `enter` on
            # the closing bar specifically, not merely for any signal.
            opened_now = any(
                sig.action == "enter" and sig.ts == last_ts for sig in signals
            )
            if not opened_now:
                continue
            side = "buy" if state.pos is Pos.LONG else "sell"
            out.append(
                EntryCandidate(
                    # The UNDERLYING, because that is what the sizer dedups on
                    # and what the runner maps to a contract for execution.
                    symbol=underlying,
                    side=side,
                    hint={
                        "signal_price": str(state.entry_price),
                        # The strategy's own protective distance, in rupees, so
                        # the stop sweep rests it at exactly the level the
                        # backtest used rather than at the bucket percent.
                        "stop_distance": str(
                            state.entry_price * state.stop_pct / Decimal("100")
                        ),
                        "signal": f"cci_recovery_{side}",
                        "signal_contract": contract.symbol,
                    },
                )
            )
        return out

    # ── exits ───────────────────────────────────────────────────────────
    def select_exits(
        self,
        held: dict[str, Position],
        data: MarketData,
        regimes=None,  # noqa: ARG002 — this strategy has no regime gate
    ) -> list[str]:
        """Close when the replayed machine says flat but we still hold.

        The stop is ALSO resting at the exchange (Decision 022), so this is the
        second of two independent paths to the same exit — and the signal exit
        at +/-250, which has no exchange-resident equivalent, only exists here.
        """
        out: list[str] = []
        for symbol in held:
            underlying = underlying_of(symbol)
            got = self._state_for(underlying, data)
            if got is None:
                continue
            state = got[0]
            # Exits are deliberately NOT transition-gated. A missed exit leaves
            # a live position the strategy believes it has closed; a repeated
            # one is a no-op once the position is gone.
            if state.pos is Pos.FLAT:
                out.append(symbol)
        return out

    def kelly_inputs(self, symbol: str) -> tuple[Decimal, Decimal] | None:  # noqa: ARG002
        """None — mu/sigma come from allocator.yaml, per the handoff."""
        return None


__all__ = ["CciGasReversion15m", "cci_series"]
