"""Unit tests for shared.allocator.caps — pure math."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.shared.allocator.caps import apply_aggregate_cap, apply_per_symbol_cap


class TestPerSymbolCap:
    def test_clamps_oversized(self) -> None:
        weights = {"A": Decimal("0.5"), "B": Decimal("0.1")}
        result = apply_per_symbol_cap(weights, Decimal("0.3"))
        assert result == {"A": Decimal("0.3"), "B": Decimal("0.1")}

    def test_passes_undersized_through(self) -> None:
        weights = {"A": Decimal("0.1"), "B": Decimal("0.2")}
        result = apply_per_symbol_cap(weights, Decimal("0.5"))
        assert result == weights

    def test_zero_cap_raises(self) -> None:
        with pytest.raises(ValueError):
            apply_per_symbol_cap({"A": Decimal("0.1")}, Decimal("0"))


class TestAggregateCap:
    def test_under_cap_no_change(self) -> None:
        weights = {"A": Decimal("0.2"), "B": Decimal("0.3")}
        result = apply_aggregate_cap(weights, Decimal("1.0"))
        assert result == weights

    def test_at_cap_no_change(self) -> None:
        weights = {"A": Decimal("0.5"), "B": Decimal("0.5")}
        result = apply_aggregate_cap(weights, Decimal("1.0"))
        assert result == weights

    def test_over_cap_scales_proportionally(self) -> None:
        # Sum = 2.0 → scale to 1.0 → each halved
        weights = {"A": Decimal("0.8"), "B": Decimal("1.2")}
        result = apply_aggregate_cap(weights, Decimal("1.0"))
        assert result == {"A": Decimal("0.4"), "B": Decimal("0.6")}
        assert sum(result.values()) == Decimal("1.0")
