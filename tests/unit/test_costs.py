"""Pre-trade cost estimation from the cited rate card (Decision 036, Phase C).

The load-bearing test is ``test_unsigned_card_refuses_to_estimate``. The user's
requirement was explicit — sign-off before any pre-trade estimate uses these
rates — and a card that merely warned would be a card that silently became
load-bearing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.shared.costs import (
    DEFAULT_CARD_PATH,
    ChargeSide,
    FeeCardNotSignedOffError,
    FeeRateCard,
    drift_ratio,
    estimate_charges,
    load_fee_card,
)


def _line(rate: str, side: str = "both") -> dict:
    return {
        "rate": rate,
        "side": side,
        "source": "https://example.test/rates",
        "verified_on": "2026-08-29",
    }


def _card(signed: bool = True) -> FeeRateCard:
    return FeeRateCard.model_validate(
        {
            "signed_off": signed,
            "segments": {
                "seg": {
                    "brokerage": {
                        "flat_per_order": "20",
                        "source": "https://example.test/brokerage",
                        "verified_on": "2026-08-29",
                    },
                    "stt": _line("0.0015", "sell"),
                    "exchange_txn": _line("0.000355299"),
                    "sebi": _line("0.000001"),
                    "stamp_duty": _line("0.00003", "buy"),
                    "gst_rate": "0.18",
                    "gst_source": "https://example.test/gst",
                    "gst_verified_on": "2026-08-29",
                }
            },
        }
    )


# ── the sign-off gate ───────────────────────────────────────────────────
def test_unsigned_card_refuses_to_estimate() -> None:
    """A refusal, not a warning and not a zero.

    An estimate silently returning 0 reads downstream as "this trade is free",
    which is worse than having no estimate at all.
    """
    with pytest.raises(FeeCardNotSignedOffError, match="not signed off"):
        estimate_charges(
            _card(signed=False),
            segment="seg",
            side="buy",
            quantity=Decimal("65"),
            price=Decimal("200"),
        )


def test_unknown_segment_raises_rather_than_estimating_zero() -> None:
    """Understating cost is worst exactly where we know least."""
    with pytest.raises(KeyError, match="no fee rates carded"):
        estimate_charges(
            _card(), segment="nope", side="buy",
            quantity=Decimal("1"), price=Decimal("1"),
        )


# ── per-line arithmetic ─────────────────────────────────────────────────
def test_sell_side_levy_is_not_charged_on_a_buy() -> None:
    """Option STT is a SELL-side levy on premium. Charging it on the buy leg
    would roughly double the estimated round-trip cost."""
    buy = estimate_charges(
        _card(), segment="seg", side="buy",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    sell = estimate_charges(
        _card(), segment="seg", side="sell",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    assert buy.stt == Decimal("0")
    # 65 * 200 = 13,000 turnover; 0.15% = 19.50
    assert sell.stt == Decimal("19.50")


def test_buy_side_levy_is_not_charged_on_a_sell() -> None:
    buy = estimate_charges(
        _card(), segment="seg", side="buy",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    sell = estimate_charges(
        _card(), segment="seg", side="sell",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    assert sell.stamp_duty == Decimal("0")
    assert buy.stamp_duty == Decimal("13000") * Decimal("0.00003")


def test_gst_applies_to_services_only_never_to_taxes() -> None:
    """GST is charged on brokerage + exchange + SEBI. Applying it to STT or
    stamp duty would be taxing a tax, and would overstate an option's cost
    most of all, since STT is its largest line."""
    got = estimate_charges(
        _card(), segment="seg", side="sell",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    expected = (got.brokerage + got.exchange_txn + got.sebi) * Decimal("0.18")
    assert got.gst == expected
    assert got.gst < got.stt  # sanity: the tax dwarfs the tax-on-service


def test_turnover_is_premium_for_an_option_not_underlying_notional() -> None:
    """The single biggest way an equity intuition mis-estimates an option:
    levies are on PREMIUM (65 x 200 = Rs 13,000), not on the underlying
    notional the lot controls (65 x 24,500 = Rs 15.9 lakh)."""
    got = estimate_charges(
        _card(), segment="seg", side="sell",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    assert got.stt == Decimal("13000") * Decimal("0.0015")


def test_flat_brokerage_dominates_a_small_lot() -> None:
    """Rs 20 on a Rs 13,000 lot is ~0.15% per leg — the reason F&O costs need
    their own estimate rather than an equity rule of thumb."""
    got = estimate_charges(
        _card(), segment="seg", side="buy",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    assert got.brokerage == Decimal("20")
    assert got.brokerage / Decimal("13000") > Decimal("0.0015")


def test_brokerage_takes_the_lower_of_flat_and_percentage() -> None:
    """Dhan prices equity intraday as "Rs 20 or 0.03%, whichever is lower"."""
    card = FeeRateCard.model_validate(
        {
            "signed_off": True,
            "segments": {
                "seg": {
                    "brokerage": {
                        "flat_per_order": "20",
                        "pct_of_turnover": "0.0003",
                        "source": "https://example.test",
                        "verified_on": "2026-08-29",
                    },
                    "stt": _line("0"),
                    "exchange_txn": _line("0"),
                    "sebi": _line("0"),
                    "stamp_duty": _line("0"),
                    "gst_rate": "0.18",
                    "gst_source": "https://example.test",
                    "gst_verified_on": "2026-08-29",
                }
            },
        }
    )
    # Small order: 0.03% of 10,000 = 3 -> the percentage wins.
    small = estimate_charges(
        card, segment="seg", side="buy",
        quantity=Decimal("10"), price=Decimal("1000"),
    )
    assert small.brokerage == Decimal("3")
    # Large order: 0.03% of 1,000,000 = 300 -> the flat cap wins.
    large = estimate_charges(
        card, segment="seg", side="buy",
        quantity=Decimal("1000"), price=Decimal("1000"),
    )
    assert large.brokerage == Decimal("20")


def test_total_sums_every_line() -> None:
    got = estimate_charges(
        _card(), segment="seg", side="sell",
        quantity=Decimal("65"), price=Decimal("200"),
    )
    assert got.total == (
        got.brokerage + got.stt + got.exchange_txn
        + got.sebi + got.stamp_duty + got.gst
    )


# ── provenance is mandatory ─────────────────────────────────────────────
def test_a_rate_without_a_source_is_rejected() -> None:
    """A rate with no citation is the guesswork this module exists to remove."""
    with pytest.raises(ValueError, match="source"):
        FeeRateCard.model_validate(
            {
                "segments": {
                    "seg": {
                        "brokerage": {
                            "flat_per_order": "20",
                            "verified_on": "2026-08-29",
                        },
                        "stt": _line("0"),
                        "exchange_txn": _line("0"),
                        "sebi": _line("0"),
                        "stamp_duty": _line("0"),
                        "gst_source": "https://example.test",
                        "gst_verified_on": "2026-08-29",
                    }
                }
            }
        )


# ── drift ───────────────────────────────────────────────────────────────
def test_drift_ratio() -> None:
    assert drift_ratio(Decimal("95"), Decimal("100")) == Decimal("0.05")
    assert drift_ratio(Decimal("105"), Decimal("100")) == Decimal("0.05")


def test_zero_actual_is_no_comparison_not_agreement() -> None:
    """A zero actual on an Indian trade is essentially impossible — STT alone
    is non-zero on one leg — so it means charges have not landed, and must not
    read as a perfect match."""
    assert drift_ratio(Decimal("20"), Decimal("0")) is None


# ── the shipped card ────────────────────────────────────────────────────
def test_repo_card_loads_and_ships_unsigned() -> None:
    """It must be inert until the user has read the sources."""
    card = load_fee_card(DEFAULT_CARD_PATH)
    assert card.signed_off is False
    assert {"nse_fno_futures", "nse_fno_options"} <= set(card.segments)


def test_every_shipped_rate_carries_a_source_and_a_date() -> None:
    card = load_fee_card(DEFAULT_CARD_PATH)
    for name, seg in card.segments.items():
        assert seg.brokerage.source.startswith("http"), name
        for line_name in ("stt", "exchange_txn", "sebi", "stamp_duty"):
            line = getattr(seg, line_name)
            assert line.source.startswith("http"), f"{name}.{line_name}"
            assert line.verified_on >= date(2026, 1, 1), f"{name}.{line_name}"
        assert seg.gst_source.startswith("http"), name


def test_shipped_fno_sides_match_how_the_levies_actually_work() -> None:
    """STT on the sell leg, stamp duty on the buy leg. Getting either side
    wrong misstates round-trip cost by the size of that line."""
    card = load_fee_card(DEFAULT_CARD_PATH)
    for name in ("nse_fno_futures", "nse_fno_options"):
        seg = card.segments[name]
        assert seg.stt.side is ChargeSide.SELL, name
        assert seg.stamp_duty.side is ChargeSide.BUY, name
        assert seg.exchange_txn.side is ChargeSide.BOTH, name
