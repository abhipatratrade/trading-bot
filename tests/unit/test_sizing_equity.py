"""Decision 027 — per-market sizing equity (Indian allocation cap).

Dhan has no sub-accounts, so Indian buckets cap sizing equity at the bucket's
allocation (capital + adjustments); crypto keeps pure live-wallet sizing
(Decision 025, sub-account isolation via Decision 019).
"""

from __future__ import annotations

from decimal import Decimal

from src.shared.allocator.sizer import sizing_equity
from src.shared.bucket import Market

_10L = Decimal("1000000")
_50K = Decimal("50000")


def test_crypto_uses_wallet_untouched() -> None:
    # Sub-account wallet IS the bucket — profits compound into sizing.
    assert sizing_equity(
        market=Market.CRYPTO, wallet_equity_inr=_10L, capital_inr=_50K
    ) == _10L


def test_indian_caps_at_allocation() -> None:
    # ₹10L sandbox wallet sizes like the real ₹50k bucket.
    assert sizing_equity(
        market=Market.INDIAN, wallet_equity_inr=_10L, capital_inr=_50K
    ) == _50K


def test_indian_wallet_below_allocation_floors_at_wallet() -> None:
    # Can never size on money the account doesn't hold.
    assert sizing_equity(
        market=Market.INDIAN, wallet_equity_inr=Decimal("30000"), capital_inr=_50K
    ) == Decimal("30000")


def test_indian_positive_adjustment_raises_cap() -> None:
    # Deliberate compounding / top-up: recorded adjustment lifts the cap.
    assert sizing_equity(
        market=Market.INDIAN,
        wallet_equity_inr=_10L,
        capital_inr=_50K,
        adjustments_inr=Decimal("10000"),
    ) == Decimal("60000")


def test_indian_negative_adjustment_lowers_cap() -> None:
    # Withdrawal from the bucket shrinks what it may size on.
    assert sizing_equity(
        market=Market.INDIAN,
        wallet_equity_inr=_10L,
        capital_inr=_50K,
        adjustments_inr=Decimal("-20000"),
    ) == Decimal("30000")
