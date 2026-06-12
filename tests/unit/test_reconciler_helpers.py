"""Tests for reconciler pure helpers.

Full integration tests for `_reconcile_positions` need a live Postgres
session and broker mock; those run against the GCP testnet rather than
the unit suite. Here we cover the pure mapping logic that previously
existed only inside the reconciler.
"""

from __future__ import annotations

import pytest

from src.core.models import PositionSide
from src.order_manager.reconciler import _exchange_side_to_position


class TestExchangeSideToPosition:
    @pytest.mark.parametrize("side", ["long", "LONG", "Long", "buy", "Buy"])
    def test_long_variants(self, side: str) -> None:
        assert _exchange_side_to_position(side) == PositionSide.LONG

    @pytest.mark.parametrize("side", ["short", "SHORT", "Short", "sell", "Sell"])
    def test_short_variants(self, side: str) -> None:
        assert _exchange_side_to_position(side) == PositionSide.SHORT

    @pytest.mark.parametrize("side", ["flat", "", "unknown", None])
    def test_other_values_default_to_flat(self, side: str | None) -> None:
        assert _exchange_side_to_position(side) == PositionSide.FLAT  # type: ignore[arg-type]
