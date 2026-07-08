"""Unit tests for src/order_manager/pnl.py — pure P&L math (Phase 1c)."""

from __future__ import annotations

from decimal import Decimal

from src.order_manager.pnl import (
    aggregate_fills,
    bucket_cumulative_pnl,
    pnl_pct,
    realized_pnl,
    trade_notional,
)

D = Decimal


class TestAggregateFills:
    def test_single_fill(self) -> None:
        agg = aggregate_fills([(D("100"), D("5"), D("0.5"))])
        assert agg is not None
        assert agg.avg_price == D("100")
        assert agg.filled_size == D("5")
        assert agg.commission == D("0.5")

    def test_volume_weighted_average(self) -> None:
        # 2 @ 100 and 8 @ 110 → avg 108
        agg = aggregate_fills(
            [(D("100"), D("2"), D("0.1")), (D("110"), D("8"), D("0.4"))]
        )
        assert agg is not None
        assert agg.avg_price == D("108")
        assert agg.filled_size == D("10")
        assert agg.commission == D("0.5")

    def test_empty_returns_none(self) -> None:
        assert aggregate_fills([]) is None

    def test_zero_size_returns_none(self) -> None:
        assert aggregate_fills([(D("100"), D("0"), D("0"))]) is None


class TestTradeNotional:
    def test_contract_size_applied(self) -> None:
        # 10 contracts of 0.001 BTC at $60,000 = $600
        assert trade_notional(D("60000"), D("10"), D("0.001")) == D("600")


class TestRealizedPnl:
    def test_long_profit(self) -> None:
        pnl = realized_pnl(
            entry_avg=D("100"),
            exit_avg=D("110"),
            size=D("10"),
            contract_size=D("0.1"),
            entry_is_long=True,
            total_fees=D("1"),
        )
        # (110-100) × 10 × 0.1 = 10, minus 1 fee = 9
        assert pnl == D("9")

    def test_long_loss(self) -> None:
        pnl = realized_pnl(
            entry_avg=D("100"),
            exit_avg=D("90"),
            size=D("10"),
            contract_size=D("0.1"),
            entry_is_long=True,
        )
        assert pnl == D("-10")

    def test_short_profit(self) -> None:
        pnl = realized_pnl(
            entry_avg=D("100"),
            exit_avg=D("90"),
            size=D("10"),
            contract_size=D("0.1"),
            entry_is_long=False,
        )
        assert pnl == D("10")


class TestPnlPct:
    def test_basic(self) -> None:
        assert pnl_pct(D("9"), D("100")) == D("9")

    def test_zero_notional_none(self) -> None:
        assert pnl_pct(D("9"), D("0")) is None


class TestBucketCumulativePnl:
    def test_profit(self) -> None:
        amt, pct = bucket_cumulative_pnl(
            capital=D("50000"), available=D("40000"), locked=D("15000")
        )
        assert amt == D("5000")
        assert pct == D("10")

    def test_loss(self) -> None:
        amt, pct = bucket_cumulative_pnl(
            capital=D("50000"), available=D("30000"), locked=D("15000")
        )
        assert amt == D("-5000")
        assert pct == D("-10")

    def test_deposit_adjustment_not_counted_as_profit(self) -> None:
        # ₹10k deposited after seed: equity 60k on 50k capital is NOT +10k pnl
        amt, pct = bucket_cumulative_pnl(
            capital=D("50000"),
            available=D("45000"),
            locked=D("15000"),
            adjustments=D("10000"),
        )
        assert amt == D("0")
        assert pct == D("0")

    def test_nonpositive_base_pct_none(self) -> None:
        amt, pct = bucket_cumulative_pnl(
            capital=D("0"), available=D("100"), locked=D("0")
        )
        assert amt == D("100")
        assert pct is None


class TestRealizedTotals:
    """Dashboard cumulative profit / loss split (header rework)."""

    def test_mixed_wins_and_losses(self) -> None:
        from src.order_manager.pnl import realized_totals

        profit, loss = realized_totals(
            [Decimal("10"), Decimal("-4"), Decimal("2.5"), Decimal("-1")]
        )
        assert profit == Decimal("12.5")
        assert loss == Decimal("-5")

    def test_empty_is_zero_zero(self) -> None:
        from src.order_manager.pnl import realized_totals

        assert realized_totals([]) == (Decimal("0"), Decimal("0"))

    def test_zero_pnl_counts_as_neither(self) -> None:
        from src.order_manager.pnl import realized_totals

        profit, loss = realized_totals([Decimal("0")])
        assert profit == Decimal("0")
        assert loss == Decimal("0")
