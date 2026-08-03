"""Consolidated trade ledger — pure builders, no DB.

The two properties worth pinning are both ways the file could quietly lie:
double-counted P&L (the reconciler stamps it on BOTH legs of a round-trip) and
silently mixed currencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.models import BrokerName, OrderSide, OrderStatus
from src.reporting.tax_ledger import (
    LedgerRow,
    build_ledger,
    financial_year,
    fy_bounds,
    summarise,
)


class _Trade:
    """Duck-types the ORM row the builders read."""

    def __init__(
        self,
        *,
        symbol: str = "BTCUSD",
        side: OrderSide = OrderSide.BUY,
        status: OrderStatus = OrderStatus.FILLED,
        broker: BrokerName = BrokerName.DELTA_INDIA,
        quantity: str = "4",
        fees: str = "0.14",
        extra: dict | None = None,
        at: datetime | None = None,
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.status = status
        self.broker = broker
        self.quantity = Decimal(quantity)
        self.price = None
        self.fees = Decimal(fees)
        self.extra = extra
        self.bucket_id = "longterm-crypto"
        self.strategy_id = "longterm-crypto"
        self.strategy_name = "ema"
        self.exchange_order_id = "X1"
        self.client_order_id = "C1"
        self.filled_at = at or datetime(2026, 7, 6, 12, 24, tzinfo=UTC)
        self.submitted_at = self.filled_at
        self.created_at = self.filled_at


_OPEN = {
    "avg_fill_price": "59656",
    "contract_size": "0.001",
    "traded_notional_usd": "238.624",
    "pnl_usd": "8.93498270",
    "closed_by_trade_id": 6044,
}
_CLOSE = {
    "avg_fill_price": "61961.5",
    "contract_size": "0.001",
    "traded_notional_usd": "247.8460",
    "pnl_usd": "8.93498270",
    "reduce_only": True,
}


# ---------------------------------------------------------------------------
# Financial year
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("day", "label"),
    [
        ((2026, 5, 2), "FY2026-27"),
        ((2026, 4, 1), "FY2026-27"),   # first day
        ((2027, 3, 31), "FY2026-27"),  # last day
        ((2026, 3, 31), "FY2025-26"),  # day before
        ((2027, 4, 1), "FY2027-28"),
    ],
)
def test_indian_financial_year_boundaries(day, label) -> None:
    from datetime import date

    assert financial_year(date(*day)) == label


def test_fy_bounds_round_trip() -> None:
    start, end = fy_bounds("FY2026-27")
    assert (start.month, start.day) == (4, 1)
    assert (end.month, end.day) == (3, 31)
    assert financial_year(start) == financial_year(end) == "FY2026-27"


# ---------------------------------------------------------------------------
# Double-counted P&L — the one that would misstate a total
# ---------------------------------------------------------------------------
def test_pnl_is_attributed_to_the_closing_leg_only() -> None:
    """The reconciler stamps pnl_usd on BOTH legs; summing both doubles it."""
    rows = build_ledger([_Trade(extra=_OPEN), _Trade(side=OrderSide.SELL, extra=_CLOSE)])
    by_leg = {r["leg"]: r for r in rows}
    assert by_leg["OPEN"]["realized_pnl"] is None
    assert by_leg["CLOSE"]["realized_pnl"] == Decimal("8.93498270")


def test_round_trip_total_is_not_doubled() -> None:
    rows = build_ledger([_Trade(extra=_OPEN), _Trade(side=OrderSide.SELL, extra=_CLOSE)])
    assert summarise(rows)["USD"]["realized"] == Decimal("8.93498270")


def test_reduce_only_marks_the_close_even_on_a_buy() -> None:
    """A short's closing leg is a BUY — side alone cannot classify it."""
    row = LedgerRow(_Trade(side=OrderSide.BUY, extra={"reduce_only": True}))
    assert row.is_closing


def test_a_long_only_equity_sell_counts_as_a_close() -> None:
    """Indian strategies are long-only and never set reduce_only."""
    row = LedgerRow(
        _Trade(broker=BrokerName.DHAN, side=OrderSide.SELL, extra={})
    )
    assert row.is_closing


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------
def test_currency_follows_the_broker() -> None:
    rows = build_ledger(
        [_Trade(), _Trade(broker=BrokerName.DHAN, symbol="SUZLON", extra={})]
    )
    assert {r["currency"] for r in rows} == {"USD", "INR"}


def test_totals_never_mix_currencies() -> None:
    """Adding USD to INR is the kind of number that ends up on a form."""
    rows = build_ledger(
        [
            _Trade(side=OrderSide.SELL, extra=_CLOSE),
            _Trade(
                broker=BrokerName.DHAN,
                symbol="SUZLON",
                side=OrderSide.SELL,
                fees="12.50",
                extra={"avg_fill_price": "48.10", "realized_pnl": "300"},
            ),
        ]
    )
    totals = summarise(rows)
    assert set(totals) == {"USD", "INR"}
    assert totals["INR"]["realized"] == Decimal("300")
    assert totals["USD"]["realized"] == Decimal("8.93498270")


# ---------------------------------------------------------------------------
# What belongs in a ledger at all
# ---------------------------------------------------------------------------
def test_rejected_and_cancelled_orders_are_excluded() -> None:
    """6,009 of 6,060 live rows are rejects from the crypto soak."""
    rows = build_ledger(
        [
            _Trade(status=OrderStatus.REJECTED, extra=_OPEN),
            _Trade(status=OrderStatus.CANCELED, extra=_OPEN),
            _Trade(status=OrderStatus.FILLED, extra=_OPEN),
        ]
    )
    assert len(rows) == 1


def test_partial_fills_are_included() -> None:
    rows = build_ledger([_Trade(status=OrderStatus.PARTIAL, extra=_OPEN)])
    assert len(rows) == 1


def test_fill_price_prefers_the_actual_fill_over_the_intent() -> None:
    """Trade.price is the intended price and is NULL on every live fill."""
    assert LedgerRow(_Trade(extra=_OPEN)).fill_price == Decimal("59656")


def test_contract_size_yields_the_base_quantity() -> None:
    """Delta quantities are CONTRACTS; 4 × 0.001 = 0.004 BTC."""
    row = build_ledger([_Trade(extra=_OPEN)])[0]
    assert row["quantity"] == Decimal("4")
    assert row["base_quantity"] == Decimal("0.004")


def test_missing_extra_does_not_crash_the_row() -> None:
    """Half the live filled rows carry no avg_fill_price at all."""
    row = build_ledger([_Trade(extra=None)])[0]
    assert row["fill_price"] is None
    assert row["realized_pnl"] is None
    assert row["symbol"] == "BTCUSD"


def test_fy_filter_selects_only_that_year() -> None:
    rows = build_ledger(
        [
            _Trade(extra=_OPEN, at=datetime(2026, 5, 2, tzinfo=UTC)),
            _Trade(extra=_OPEN, at=datetime(2026, 3, 2, tzinfo=UTC)),
        ],
        fy="FY2026-27",
    )
    assert len(rows) == 1
    assert rows[0]["date_ist"] == "2026-05-02"


def test_rows_are_ordered_oldest_first() -> None:
    rows = build_ledger(
        [
            _Trade(extra=_OPEN, at=datetime(2026, 7, 6, tzinfo=UTC)),
            _Trade(extra=_OPEN, at=datetime(2026, 5, 2, tzinfo=UTC)),
        ]
    )
    assert [r["date_ist"] for r in rows] == ["2026-05-02", "2026-07-06"]
