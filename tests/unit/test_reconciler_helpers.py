"""Tests for reconciler pure helpers.

Full integration tests for `_reconcile_positions` need a live Postgres
session and broker mock; those run against the GCP testnet rather than
the unit suite. Here we cover the pure mapping logic that previously
existed only inside the reconciler.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.models import PositionSide, Trade
from src.order_manager.reconciler import _exchange_side_to_position, _filled_size


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


class TestFilledSize:
    """A synthetic exit carries no ``filled_size`` — and used to poison P&L.

    ``_enrich_trades_pnl`` read ``extra["filled_size"]`` with a bracket, but a
    synthetic exit is written for a position that vanished from the broker with
    no order of ours behind it, so there are no fills to aggregate and the key
    is simply absent. The KeyError escaped the whole Pass 2 loop, so ONE such
    row zeroed realized P&L for every trade in the sweep — which is why the
    live EOD digest reported "Realized Rs +0.00" while real round-trips (IIFL
    2026-08-25, PIIND 2026-08-18) sat closed in the ledger.
    """

    def test_prefers_the_stamped_fill_size(self) -> None:
        t = Trade(quantity=Decimal("74"), extra={"filled_size": "70"})
        assert _filled_size(t) == Decimal("70")

    def test_falls_back_to_quantity_for_a_synthetic_exit(self) -> None:
        t = Trade(
            quantity=Decimal("74"),
            extra={
                "reduce_only": True,
                "synthetic_exit": True,
                "avg_fill_price": "669.15",
            },
        )
        assert _filled_size(t) == Decimal("74")

    def test_none_when_size_is_unknowable(self) -> None:
        assert _filled_size(Trade(quantity=None, extra={})) is None

    @pytest.mark.parametrize("qty", [Decimal("0"), Decimal("-3")])
    def test_non_positive_size_is_not_a_size(self, qty: Decimal) -> None:
        """min() over a zero would silently report a zero-P&L round trip."""
        assert _filled_size(Trade(quantity=qty, extra={})) is None

    def test_unparseable_stamp_falls_back_rather_than_raising(self) -> None:
        t = Trade(quantity=Decimal("12"), extra={"filled_size": ""})
        assert _filled_size(t) == Decimal("12")
