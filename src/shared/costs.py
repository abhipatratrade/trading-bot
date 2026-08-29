"""
Pre-trade cost estimation from a CITED rate card (Decision 036, Phase C).

There are two honest ways to know what a trade cost, and this repo now has
both. The authoritative one already existed: ``DhanClient.get_order_charges``
reads the venue's own trade-history report and the reconciler stamps those
real billed rupees onto ``Trade.fees``. Nothing here replaces that — actuals
remain the booked truth.

What was missing is a BEFORE. Actuals arrive at end of day, and a sizer that
cannot estimate cost before placing cannot tell a trade whose edge survives
costs from one whose edge does not. That gap is wider in F&O than it ever was
in cash equity: Dhan's flat ₹20 per order is ~0.15% round-trip on a ₹13,000
option lot against ~0.05% on a ₹50,000 equity slot, and option STT is charged
on the SELL side at 0.15% of premium, which no equity intuition prepares you
for.

**Every rate here is cited and dated, and the card is inert until signed off.**
``estimate_charges`` refuses to run against an unsigned card — not a warning, a
refusal — because a number nobody checked is exactly the kind of thing that
silently becomes load-bearing. The user signs the card after reading the
sources; until then the drift check simply does not run.

The card can be validated before it is ever trusted. swing-indian and
intraday-indian have been trading cash equity on this same Dhan account for
months, and every one of those trades has real billed charges in the ledger.
``scripts/fee_card_reconcile.py`` replays the card against them, so "trust
these rates" becomes "here is the card reconciling against real Dhan bills".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from src.brokers.base import OrderCharges

# Repo root — the card sits beside buckets.yaml, for the same reason: it is
# policy the user edits and audits, not code.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CARD_PATH = _REPO_ROOT / "fee_rates.yaml"


class FeeCardNotSignedOffError(Exception):
    """Raised when an estimate is asked of a card nobody has signed.

    A hard refusal rather than a zero or a warning: an estimate silently
    returning 0 would read downstream as "this trade is free", which is worse
    than having no estimate at all.
    """


class ChargeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"
    BOTH = "both"


class RateLine(BaseModel):
    """One statutory or exchange levy, with its provenance attached.

    ``source`` and ``verified_on`` are REQUIRED. A rate without a citation is
    the guesswork this module exists to eliminate, and these change more often
    than anyone expects — the F&O STT rates below moved on 1 April 2026.
    """

    rate: Decimal = Field(ge=0)  # fraction, e.g. 0.0005 for 0.05%
    side: ChargeSide = ChargeSide.BOTH
    source: str = Field(min_length=1)
    verified_on: date
    note: str = ""

    def applies_to(self, side: str) -> bool:
        if self.side is ChargeSide.BOTH:
            return True
        return side.lower() == self.side.value

    def charge(self, turnover: Decimal, side: str) -> Decimal:
        return turnover * self.rate if self.applies_to(side) else Decimal("0")


class BrokerageLine(BaseModel):
    """The broker's own fee — the one negotiable component.

    Both fields present means "whichever is LOWER", which is how Dhan prices
    equity intraday (₹20 or 0.03%). F&O is a flat ₹20 with no percentage
    alternative, so ``pct_of_turnover`` is simply absent there.
    """

    flat_per_order: Decimal | None = Field(default=None, ge=0)
    pct_of_turnover: Decimal | None = Field(default=None, ge=0)
    source: str = Field(min_length=1)
    verified_on: date
    note: str = ""

    def charge(self, turnover: Decimal) -> Decimal:
        flat = self.flat_per_order
        pct = self.pct_of_turnover * turnover if self.pct_of_turnover else None
        if flat is not None and pct is not None:
            return min(flat, pct)
        if flat is not None:
            return flat
        return pct or Decimal("0")


class SegmentRates(BaseModel):
    """Every charge for one (exchange, segment, product) combination."""

    label: str = ""
    brokerage: BrokerageLine
    stt: RateLine
    exchange_txn: RateLine
    sebi: RateLine
    stamp_duty: RateLine
    # GST applies to the SERVICE components only — brokerage, exchange
    # transaction charges and the SEBI fee. Never to STT or stamp duty, which
    # are themselves taxes.
    gst_rate: Decimal = Field(default=Decimal("0.18"), ge=0)
    gst_source: str = Field(min_length=1)
    gst_verified_on: date


class FeeRateCard(BaseModel):
    """The whole card, plus its sign-off state.

    ``signed_off`` gates every estimate. It ships false, and the user flips it
    after reading the per-line sources — which is the whole point of carrying
    a ``source`` on each line rather than a single citation for the file.
    """

    signed_off: bool = False
    signed_off_by: str = ""
    signed_off_on: date | None = None
    note: str = ""
    segments: dict[str, SegmentRates]

    def segment(self, name: str) -> SegmentRates | None:
        return self.segments.get(name)


def load_fee_card(path: Path | None = None) -> FeeRateCard:
    """Load and validate the rate card. Fail-fast on a malformed file."""
    p = path or DEFAULT_CARD_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return FeeRateCard.model_validate(raw)


def estimate_charges(
    card: FeeRateCard,
    *,
    segment: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
) -> OrderCharges:
    """What ONE order is expected to cost, before it is placed.

    Returns the same ``OrderCharges`` breakdown the venue reports afterwards,
    so an estimate and an actual are directly comparable line by line — which
    is what makes the drift check possible at all.

    ``price`` is the traded price of the instrument: the premium for an option,
    the futures price for a future, the share price for cash equity. Turnover
    is ``quantity × price`` in every case, which is also how the exchange
    computes option levies (on premium value, not on the underlying notional —
    the single biggest way an equity intuition mis-estimates an option).

    Raises:
        FeeCardNotSignedOffError: if nobody has signed the card.
        KeyError: if the segment is not on the card. Deliberate — silently
            estimating zero for an unknown segment would understate cost
            exactly where we know least.
    """
    if not card.signed_off:
        raise FeeCardNotSignedOffError(
            "fee_rates.yaml is not signed off; no pre-trade estimate may use "
            "it. Read the per-line sources, then set signed_off: true."
        )
    rates = card.segment(segment)
    if rates is None:
        raise KeyError(f"no fee rates carded for segment {segment!r}")

    turnover = quantity * price
    brokerage = rates.brokerage.charge(turnover)
    exchange_txn = rates.exchange_txn.charge(turnover, side)
    sebi = rates.sebi.charge(turnover, side)
    return OrderCharges(
        exchange_order_id="",
        brokerage=brokerage,
        stt=rates.stt.charge(turnover, side),
        exchange_txn=exchange_txn,
        sebi=sebi,
        stamp_duty=rates.stamp_duty.charge(turnover, side),
        # Service tax on the service components only.
        gst=(brokerage + exchange_txn + sebi) * rates.gst_rate,
    )


def drift_ratio(estimated: Decimal, actual: Decimal) -> Decimal | None:
    """``|actual - estimated| / actual``, or None when actual is zero.

    None means "nothing to compare against", not "no drift": a zero actual on
    an Indian equity or F&O trade is essentially impossible (STT alone is
    non-zero on one leg), so it signals charges that have not landed yet
    rather than a free trade. The caller must not read it as agreement.
    """
    if actual == 0:
        return None
    return abs(actual - estimated) / abs(actual)
