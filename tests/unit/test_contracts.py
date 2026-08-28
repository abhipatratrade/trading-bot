"""Contract symbol grammar — mint, parse, and the dedup key (Decision 036).

The test that earns its keep here is
``test_hyphenated_cash_ticker_is_not_mangled``. The live swing-indian universe
holds ``NAM-INDIA``, and the obvious implementation of ``underlying_of`` —
``symbol.split("-")[0]`` — turns it into ``NAM``. That would silently break the
dedup gate for a real, currently-traded name, in a bucket running real money.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.shared.contracts import (
    ContractKey,
    contract_symbol,
    format_strike,
    is_derivative,
    parse_contract_symbol,
    underlying_of,
)


# ── minting ─────────────────────────────────────────────────────────────
def test_option_symbol_carries_the_full_expiry_date() -> None:
    assert (
        contract_symbol(
            "NIFTY", date(2026, 9, 8), strike=Decimal("23150"), option_type="CE"
        )
        == "NIFTY-20260908-23150-CE"
    )


def test_future_symbol() -> None:
    assert contract_symbol("NIFTY", date(2026, 9, 29)) == "NIFTY-20260929-FUT"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("23150.00000"), "23150"),
        (Decimal("42.50000"), "42.5"),
        (Decimal("1"), "1"),
        (Decimal("2.5"), "2.5"),
    ],
)
def test_strike_formatting(raw: Decimal, expected: str) -> None:
    """A whole Decimal normalises to exponent form (2.315E+4) unless requantised."""
    assert format_strike(raw) == expected


# ── parsing ─────────────────────────────────────────────────────────────
def test_parse_option() -> None:
    key = parse_contract_symbol("NIFTY-20260908-23150-CE")
    assert key == ContractKey(
        underlying="NIFTY",
        expiry=date(2026, 9, 8),
        strike=Decimal("23150"),
        option_type="CE",
    )
    assert not key.is_future


def test_parse_future() -> None:
    key = parse_contract_symbol("SBIN-20260929-FUT")
    assert key is not None
    assert key.is_future
    assert key.strike is None and key.option_type is None


def test_parse_fractional_strike() -> None:
    key = parse_contract_symbol("IDEA-20260929-42.5-PE")
    assert key is not None and key.strike == Decimal("42.5")


def test_round_trip_through_the_key() -> None:
    for sym in (
        "NIFTY-20260908-23150-CE",
        "IDEA-20260929-42.5-PE",
        "SBIN-20260929-FUT",
    ):
        key = parse_contract_symbol(sym)
        assert key is not None and key.symbol == sym


@pytest.mark.parametrize(
    "symbol",
    [
        "SWIGGY",
        "NAM-INDIA",
        "GVT&D",
        "NIFTY-2026-23150-CE",       # expiry not 8 digits
        "NIFTY-20261301-23150-CE",   # 8 digits, not a real date
        "NIFTY-20260908-23150-XX",   # not a leg type we accept
        "NIFTY-20260908-CE",         # option without a strike
        "",
    ],
)
def test_non_contracts_do_not_parse(symbol: str) -> None:
    assert parse_contract_symbol(symbol) is None
    assert not is_derivative(symbol)


# ── the dedup key ───────────────────────────────────────────────────────
def test_hyphenated_cash_ticker_is_not_mangled() -> None:
    """``symbol.split("-")[0]`` would return "NAM" — and NAM-INDIA is live."""
    assert underlying_of("NAM-INDIA") == "NAM-INDIA"


def test_underlying_of_is_identity_for_cash_equity() -> None:
    """Why every caller can use it unconditionally: no F&O branch to forget."""
    for sym in ("SWIGGY", "RELIANCE", "GVT&D", "BTCUSD"):
        assert underlying_of(sym) == sym


def test_underlying_of_collapses_strikes_to_one_name() -> None:
    """Two strikes on one index are ONE bet with two spellings.

    This is the property the sizer's dedup gate depends on: without it a
    strategy holding one NIFTY strike reads a second as an unrelated name.
    """
    symbols = [
        "NIFTY-20260908-23150-CE",
        "NIFTY-20260908-23200-CE",
        "NIFTY-20260915-23150-PE",
        "NIFTY-20260929-FUT",
    ]
    assert {underlying_of(s) for s in symbols} == {"NIFTY"}


def test_underlying_survives_a_hyphenated_derivative() -> None:
    """Greedy match, so a hyphenated underlying stays whole if one ever lists."""
    assert underlying_of("NAM-INDIA-20260929-FUT") == "NAM-INDIA"


def test_eight_digit_strike_does_not_shadow_the_expiry() -> None:
    """The alternation must backtrack rather than read the strike as the date."""
    key = parse_contract_symbol("X-20260929-12345678-CE")
    assert key is not None
    assert key.expiry == date(2026, 9, 29)
    assert key.strike == Decimal("12345678")
