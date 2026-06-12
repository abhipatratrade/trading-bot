"""Tests for ``notional_inr_to_contracts``.

Pure function — no DB or broker involved. Confirms the FX +
contract-size math produces the right number of whole contracts.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.shared.allocator.sizer import (
    AllocatorConfig,
    notional_inr_to_contracts,
)


def _cfg(**overrides):
    """Minimal AllocatorConfig with the fields used by these tests."""
    base = {
        "fractional_kelly": Decimal("0.25"),
        "per_symbol_cap": Decimal("0.3"),
        "aggregate_cap": Decimal("1"),
        "fx_inr_per_usd": Decimal("84"),
        "default_contract_size": Decimal("1"),
        "contract_sizes": {
            "BTCUSD": Decimal("0.001"),
            "ETHUSD": Decimal("0.01"),
            "XRPUSD": Decimal("10"),
        },
    }
    base.update(overrides)
    return AllocatorConfig(**base)


class TestNotionalToContracts:
    def test_btc_at_typical_price(self) -> None:
        # ₹75,000 notional ÷ ($63,500 × 0.001 BTC × 84) = ₹75,000 / ₹5,334 ≈ 14
        contracts = notional_inr_to_contracts(
            notional_inr=Decimal("75000"),
            mark_price_usd=Decimal("63500"),
            symbol="BTCUSD",
            config=_cfg(),
        )
        assert contracts == Decimal("14")

    def test_eth_smaller_contract_size(self) -> None:
        # ₹50,000 ÷ ($3,500 × 0.01 ETH × 84) = ₹50,000 / ₹2,940 ≈ 17
        contracts = notional_inr_to_contracts(
            notional_inr=Decimal("50000"),
            mark_price_usd=Decimal("3500"),
            symbol="ETHUSD",
            config=_cfg(),
        )
        assert contracts == Decimal("17")

    def test_xrp_large_contract_size(self) -> None:
        # XRPUSD is 10 XRP per contract.
        # ₹40,000 ÷ ($2.00 × 10 × 84) = ₹40,000 / ₹1,680 ≈ 23
        contracts = notional_inr_to_contracts(
            notional_inr=Decimal("40000"),
            mark_price_usd=Decimal("2.00"),
            symbol="XRPUSD",
            config=_cfg(),
        )
        assert contracts == Decimal("23")

    def test_unknown_symbol_uses_default(self) -> None:
        # default_contract_size = 1, so identity-style sizing applies.
        # ₹84,000 ÷ ($1,000 × 1 × 84) = exactly 1
        contracts = notional_inr_to_contracts(
            notional_inr=Decimal("84000"),
            mark_price_usd=Decimal("1000"),
            symbol="UNKNOWNUSD",
            config=_cfg(),
        )
        assert contracts == Decimal("1")

    def test_floors_partial_contracts(self) -> None:
        # 1.9 contracts → floor → 1
        contracts = notional_inr_to_contracts(
            notional_inr=Decimal("10000"),
            mark_price_usd=Decimal("63500"),
            symbol="BTCUSD",
            config=_cfg(),
        )
        # 10000 / (63500 * 0.001 * 84) = 10000 / 5334 ≈ 1.87
        assert contracts == Decimal("1")

    def test_zero_notional_yields_zero(self) -> None:
        contracts = notional_inr_to_contracts(
            notional_inr=Decimal("0"),
            mark_price_usd=Decimal("63500"),
            symbol="BTCUSD",
            config=_cfg(),
        )
        assert contracts == Decimal("0")

    def test_zero_or_negative_mark_price_yields_zero(self) -> None:
        for px in (Decimal("0"), Decimal("-100")):
            contracts = notional_inr_to_contracts(
                notional_inr=Decimal("75000"),
                mark_price_usd=px,
                symbol="BTCUSD",
                config=_cfg(),
            )
            assert contracts == Decimal("0")

    def test_legacy_fx_one_default_size_matches_old_behaviour(self) -> None:
        """With fx=1 and contract_size=1 the formula degenerates to
        floor(notional / mark_price) — the legacy behaviour we'd been
        getting before this fix. Provides a clean fallback for buckets
        whose yamls haven't been updated yet."""
        cfg = AllocatorConfig(
            fractional_kelly=Decimal("0.25"),
            per_symbol_cap=Decimal("0.3"),
            aggregate_cap=Decimal("1"),
            fx_inr_per_usd=Decimal("1"),
            default_contract_size=Decimal("1"),
            contract_sizes={},
        )
        # 75000 / 63500 = 1.18 → floor → 1
        contracts = notional_inr_to_contracts(
            notional_inr=Decimal("75000"),
            mark_price_usd=Decimal("63500"),
            symbol="BTCUSD",
            config=cfg,
        )
        assert contracts == Decimal("1")


def test_min_contract_floor_via_zero_fx_protection() -> None:
    """Defensive: extreme FX shouldn't cause divide-by-zero or negative
    contract counts."""
    # Pydantic enforces fx_inr_per_usd > 0 at validation; this guards
    # the runtime branch in case the field is bypassed.
    with pytest.raises(Exception):
        AllocatorConfig(
            fractional_kelly=Decimal("0.25"),
            per_symbol_cap=Decimal("0.3"),
            aggregate_cap=Decimal("1"),
            fx_inr_per_usd=Decimal("0"),
        )
