"""Unit tests for shared.allocator.kelly — pure math."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.shared.allocator.kelly import fractional_kelly, kelly_fraction


class TestKellyFraction:
    def test_basic_positive_edge(self) -> None:
        # μ=0.001, σ=0.02 → f* = 0.001 / 0.0004 = 2.5
        result = kelly_fraction(Decimal("0.001"), Decimal("0.02"))
        assert result == Decimal("2.5")

    def test_zero_mu_returns_zero(self) -> None:
        assert kelly_fraction(Decimal("0"), Decimal("0.02")) == Decimal("0")

    def test_negative_mu_clamps_to_zero(self) -> None:
        # Long-only: negative edge → don't trade. Decision 015.
        assert kelly_fraction(Decimal("-0.01"), Decimal("0.02")) == Decimal("0")

    def test_zero_sigma_raises(self) -> None:
        with pytest.raises(ValueError, match="sigma must be > 0"):
            kelly_fraction(Decimal("0.001"), Decimal("0"))

    def test_negative_sigma_raises(self) -> None:
        with pytest.raises(ValueError, match="sigma must be > 0"):
            kelly_fraction(Decimal("0.001"), Decimal("-0.02"))


class TestFractionalKelly:
    def test_quarter_kelly(self) -> None:
        # 1.0 full Kelly × 0.25 = 0.25
        result = fractional_kelly(Decimal("1.0"), Decimal("0.25"))
        assert result == Decimal("0.25")

    def test_default_fraction_is_quarter(self) -> None:
        assert fractional_kelly(Decimal("4.0")) == Decimal("1.0")

    def test_negative_fraction_raises(self) -> None:
        with pytest.raises(ValueError, match="fraction must be"):
            fractional_kelly(Decimal("1.0"), Decimal("-0.5"))
