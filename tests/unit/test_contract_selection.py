"""Contract selection — spot signal to one instrument (Decision 036, Phase B).

Phase B's gate is that a signal resolves to ONE contract, deterministically and
reproducibly: same inputs plus same scrip master must always yield the same
contract, or a live fill cannot be compared against the backtest that justified
it. ``test_selection_is_deterministic`` and the tie-break tests are that gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.shared.contract_selection import (
    ContractSelectionConfig,
    ContractSelector,
    ExpiryRule,
    Instrument,
    OptionSide,
    StrikeRule,
    contract_hint,
    load_contract_selection,
    monthly_expiries,
)

TODAY = date(2026, 9, 1)

# A realistic NIFTY ladder: four weeklies then a monthly, mirroring what the
# scrip master actually lists.
W1 = date(2026, 9, 8)
W2 = date(2026, 9, 15)
W3 = date(2026, 9, 22)
M1 = date(2026, 9, 29)   # last of September -> the monthly
M2 = date(2026, 10, 27)  # last of October  -> the monthly


@dataclass(frozen=True, slots=True)
class FakeContract:
    symbol: str
    underlying: str
    instrument: str
    expiry: date
    strike: Decimal | None
    option_type: str | None
    lot_size: int = 65
    security_id: str = "1"


def _opt(expiry: date, strike: int, opt: str) -> FakeContract:
    return FakeContract(
        symbol=f"NIFTY-{expiry:%Y%m%d}-{strike}-{opt}",
        underlying="NIFTY",
        instrument="OPTIDX",
        expiry=expiry,
        strike=Decimal(strike),
        option_type=opt,
    )


def _fut(expiry: date) -> FakeContract:
    return FakeContract(
        symbol=f"NIFTY-{expiry:%Y%m%d}-FUT",
        underlying="NIFTY",
        instrument="FUTIDX",
        expiry=expiry,
        strike=None,
        option_type=None,
    )


class FakeSource:
    """A ladder of 24000..25000 in 100-point steps across five expiries."""

    def __init__(
        self,
        expiries: list[date] | None = None,
        strikes: list[int] | None = None,
    ) -> None:
        self._expiries = expiries if expiries is not None else [W1, W2, W3, M1, M2]
        self._strikes = strikes if strikes is not None else list(
            range(24000, 25100, 100)
        )

    def expiries(self, underlying: str, *, instrument: str | None = None) -> list[date]:  # noqa: ARG002
        return list(self._expiries)

    def chain(
        self, underlying: str, expiry: date, *, option_type: str | None = None  # noqa: ARG002
    ) -> list[FakeContract]:
        out = [
            _opt(expiry, k, side)
            for k in self._strikes
            for side in ("CE", "PE")
            if option_type is None or side == option_type
        ]
        # Deliberately shuffled-ish: the selector must impose its own order.
        return list(reversed(out))

    def futures(self, underlying: str) -> list[FakeContract]:  # noqa: ARG002
        return [_fut(e) for e in reversed(self._expiries)]


def _cfg(**kw) -> ContractSelectionConfig:
    base = {"instrument": Instrument.OPTION}
    base.update(kw)
    return ContractSelectionConfig(**base)


def _sel(config: ContractSelectionConfig, source: FakeSource | None = None):
    return ContractSelector(source or FakeSource(), config)


# ── config validation ───────────────────────────────────────────────────
def test_delta_rule_is_refused_not_silently_downgraded() -> None:
    """A 0.30-delta strangle sized as if it were ATM is a different trade."""
    with pytest.raises(ValidationError, match="greeks"):
        _cfg(strike_rule=StrikeRule.DELTA)


@pytest.mark.parametrize(
    "rule", [StrikeRule.OTM_PCT, StrikeRule.ITM_PCT, StrikeRule.OTM_STEPS]
)
def test_rules_needing_a_value_reject_a_missing_one(rule: StrikeRule) -> None:
    with pytest.raises(ValidationError, match="strike_value"):
        _cfg(strike_rule=rule)


def test_expiry_window_must_be_coherent() -> None:
    with pytest.raises(ValidationError, match="max_days_to_expiry"):
        _cfg(min_days_to_expiry=10, max_days_to_expiry=3)


def test_yaml_loads_nested_or_flat(tmp_path) -> None:
    flat = tmp_path / "flat.yaml"
    flat.write_text("instrument: option\nstrike_rule: atm\n", encoding="utf-8")
    nested = tmp_path / "nested.yaml"
    nested.write_text(
        "contract_selection:\n  instrument: option\n  strike_rule: atm\n",
        encoding="utf-8",
    )
    assert load_contract_selection(flat) == load_contract_selection(nested)


# ── strike rules ────────────────────────────────────────────────────────
def test_atm_picks_the_nearest_listed_strike() -> None:
    got = _sel(_cfg()).select("NIFTY", spot=Decimal("24537"), side="buy", on=TODAY)
    assert got.ok
    assert got.contract.strike == Decimal("24500")
    assert got.contract.option_type == "CE"


def test_atm_tie_breaks_to_the_lower_strike() -> None:
    """Index ladders are evenly spaced, so an exact midpoint is not exotic —
    and 'whichever the sort yielded' would make one signal pick two contracts
    on two runs."""
    got = _sel(_cfg()).select("NIFTY", spot=Decimal("24550"), side="buy", on=TODAY)
    assert got.contract.strike == Decimal("24500")


def test_otm_pct_moves_away_from_the_money_per_leg() -> None:
    cfg = _cfg(strike_rule=StrikeRule.OTM_PCT, strike_value=Decimal("2"))
    # Call: OTM is ABOVE spot. 24000 * 1.02 = 24480 -> nearest 24500.
    call = _sel(cfg).select("NIFTY", spot=Decimal("24000"), side="buy", on=TODAY)
    assert call.contract.strike == Decimal("24500")
    # Put: OTM is BELOW spot. 24500 * 0.98 = 24010 -> nearest 24000.
    put = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="sell", on=TODAY)
    assert put.contract.option_type == "PE"
    assert put.contract.strike == Decimal("24000")


def test_itm_pct_is_the_mirror_of_otm() -> None:
    cfg = _cfg(strike_rule=StrikeRule.ITM_PCT, strike_value=Decimal("2"))
    call = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="buy", on=TODAY)
    # ITM for a call is BELOW spot: 24500 * 0.98 = 24010 -> 24000.
    assert call.contract.strike == Decimal("24000")


def test_otm_steps_counts_listed_strikes_not_points() -> None:
    cfg = _cfg(strike_rule=StrikeRule.OTM_STEPS, strike_value=Decimal("2"))
    call = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="buy", on=TODAY)
    assert call.contract.strike == Decimal("24700")
    put = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="sell", on=TODAY)
    assert put.contract.strike == Decimal("24300")


def test_otm_steps_off_the_end_of_the_ladder_is_a_miss_not_a_clamp() -> None:
    """Silently clamping to the last listed strike would return a contract at a
    completely different moneyness than the config asked for."""
    cfg = _cfg(strike_rule=StrikeRule.OTM_STEPS, strike_value=Decimal("50"))
    got = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="buy", on=TODAY)
    assert not got.ok
    assert "otm_steps" in got.reason


# ── option leg ──────────────────────────────────────────────────────────
def test_directional_maps_buy_to_call_and_sell_to_put() -> None:
    sel = _sel(_cfg(option_side=OptionSide.DIRECTIONAL))
    spot = Decimal("24500")
    assert sel.select("NIFTY", spot=spot, side="buy", on=TODAY).contract.option_type == "CE"
    assert sel.select("NIFTY", spot=spot, side="sell", on=TODAY).contract.option_type == "PE"


def test_explicit_leg_overrides_direction() -> None:
    """A short-put strategy is long-biased but always trades the PE leg."""
    sel = _sel(_cfg(option_side=OptionSide.PUT))
    for side in ("buy", "sell"):
        got = sel.select("NIFTY", spot=Decimal("24500"), side=side, on=TODAY)
        assert got.contract.option_type == "PE"


# ── expiry rules ────────────────────────────────────────────────────────
def test_monthly_expiries_are_derived_from_what_is_listed() -> None:
    """NSE has moved its expiry weekday more than once; whatever is listed
    last in a month IS that month's monthly."""
    assert monthly_expiries([W1, W2, W3, M1, M2]) == {M1, M2}


def test_nearest_takes_the_first_expiry_past_the_floor() -> None:
    got = _sel(_cfg(expiry_rule=ExpiryRule.NEAREST, min_days_to_expiry=2)).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert got.contract.expiry == W1


def test_min_dte_skips_an_expiry_that_is_too_close() -> None:
    """An option at 0-1 DTE is a different instrument from the same strike at 7."""
    got = _sel(_cfg(min_days_to_expiry=10)).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert got.contract.expiry == W2  # W1 is 7 days out


def test_monthly_rule_skips_the_weeklies() -> None:
    got = _sel(_cfg(expiry_rule=ExpiryRule.MONTHLY)).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert got.contract.expiry == M1


def test_weekly_rule_skips_the_monthly() -> None:
    got = _sel(_cfg(expiry_rule=ExpiryRule.WEEKLY)).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert got.contract.expiry == W1


def test_weekly_falls_back_to_monthly_when_none_are_listed() -> None:
    """Stock F&O lists no weeklies. Without this fallback a weekly-configured
    strategy would silently trade nothing on 228 of 233 underlyings."""
    source = FakeSource(expiries=[M1, M2])
    got = _sel(_cfg(expiry_rule=ExpiryRule.WEEKLY), source).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert got.ok and got.contract.expiry == M1


def test_max_dte_bounds_the_far_end() -> None:
    got = _sel(_cfg(min_days_to_expiry=2, max_days_to_expiry=10)).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert got.contract.expiry == W1


def test_no_expiry_in_window_is_a_reasoned_miss() -> None:
    got = _sel(_cfg(min_days_to_expiry=400)).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert not got.ok
    assert "expiry in window" in got.reason


# ── futures ─────────────────────────────────────────────────────────────
def test_future_selection_takes_the_front_month() -> None:
    cfg = ContractSelectionConfig(instrument=Instrument.FUTURE)
    got = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="buy", on=TODAY)
    assert got.ok
    assert got.contract.instrument == "FUTIDX"
    assert got.contract.expiry == W1
    assert got.contract.strike is None


def test_future_respects_the_dte_floor() -> None:
    cfg = ContractSelectionConfig(
        instrument=Instrument.FUTURE, min_days_to_expiry=30
    )
    got = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="buy", on=TODAY)
    assert got.contract.expiry == M2


# ── misses and determinism ──────────────────────────────────────────────
def test_missing_spot_is_a_miss_not_an_arbitrary_strike() -> None:
    got = _sel(_cfg()).select("NIFTY", spot=Decimal("0"), side="buy", on=TODAY)
    assert not got.ok
    assert "spot" in got.reason


def test_empty_chain_is_a_reasoned_miss() -> None:
    got = _sel(_cfg(), FakeSource(strikes=[])).select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=TODAY
    )
    assert not got.ok
    assert "no CE strikes" in got.reason


def test_selection_is_deterministic() -> None:
    """Phase B's gate. The source deliberately returns its chain in a
    scrambled order; the selector must impose a total order of its own."""
    cfg = _cfg(strike_rule=StrikeRule.OTM_STEPS, strike_value=Decimal("3"))
    picks = {
        _sel(cfg, FakeSource())
        .select("NIFTY", spot=Decimal("24537"), side="buy", on=TODAY)
        .contract.symbol
        for _ in range(25)
    }
    assert len(picks) == 1


def test_a_rolling_date_moves_the_expiry_forward() -> None:
    """Reproducible does not mean frozen: as W1 falls inside the DTE floor the
    selection must roll to W2, which is how a live strategy avoids entering a
    contract that is about to die."""
    sel = _sel(_cfg(min_days_to_expiry=3))
    early = sel.select("NIFTY", spot=Decimal("24500"), side="buy", on=TODAY)
    late = sel.select(
        "NIFTY", spot=Decimal("24500"), side="buy", on=W1 - timedelta(days=1)
    )
    assert early.contract.expiry == W1
    assert late.contract.expiry == W2


# ── audit payload ───────────────────────────────────────────────────────
def test_contract_hint_records_what_was_chosen() -> None:
    """Without this, a fill cannot be traced back to the rule that chose it."""
    got = _sel(_cfg()).select("NIFTY", spot=Decimal("24537"), side="buy", on=TODAY)
    hint = contract_hint(got.contract)
    assert hint["contract"] == "NIFTY-20260908-24500-CE"
    assert hint["contract_underlying"] == "NIFTY"
    assert hint["contract_expiry"] == "2026-09-08"
    assert hint["contract_strike"] == "24500"
    assert hint["contract_option_type"] == "CE"
    assert hint["contract_lot_size"] == "65"


def test_contract_hint_omits_what_a_future_does_not_have() -> None:
    cfg = ContractSelectionConfig(instrument=Instrument.FUTURE)
    got = _sel(cfg).select("NIFTY", spot=Decimal("24500"), side="buy", on=TODAY)
    hint = contract_hint(got.contract)
    assert "contract_strike" not in hint
    assert "contract_option_type" not in hint
