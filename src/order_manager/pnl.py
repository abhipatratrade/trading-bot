"""
Pure P&L math — no DB, no broker calls (importable by the backtester).

Used by:
    - the reconciler's fill-ingestion step (per-trade realized/unrealized
      P&L stored on ``Trade.extra``), and
    - the dashboard's cumulative bucket P&L card.

Conventions:
    - All prices/sizes are Decimal.
    - "notional" = avg fill price × size × contract size (quote currency,
      USD on Delta India).
    - P&L percentages are expressed against traded notional, not margin —
      multiply by leverage to read margin-relative return.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class FillAggregate:
    """Volume-weighted summary of all fills for one exchange order."""

    avg_price: Decimal
    filled_size: Decimal
    commission: Decimal


def aggregate_fills(
    fills: list[tuple[Decimal, Decimal, Decimal]],
) -> FillAggregate | None:
    """Aggregate (price, size, commission) tuples into one summary.

    Returns None when there are no fills or total size is 0.
    """
    if not fills:
        return None
    total_size = sum((size for _, size, _ in fills), Decimal("0"))
    if total_size <= 0:
        return None
    notional = sum((price * size for price, size, _ in fills), Decimal("0"))
    commission = sum((c for _, _, c in fills), Decimal("0"))
    return FillAggregate(
        avg_price=notional / total_size,
        filled_size=total_size,
        commission=commission,
    )


def trade_notional(
    avg_price: Decimal, size: Decimal, contract_size: Decimal
) -> Decimal:
    """Traded amount in quote currency for one order."""
    return avg_price * size * contract_size


def realized_pnl(
    *,
    entry_avg: Decimal,
    exit_avg: Decimal,
    size: Decimal,
    contract_size: Decimal,
    entry_is_long: bool,
    total_fees: Decimal = Decimal("0"),
) -> Decimal:
    """Realized P&L (quote currency) for a closed round-trip, net of fees."""
    direction = Decimal("1") if entry_is_long else Decimal("-1")
    gross = (exit_avg - entry_avg) * size * contract_size * direction
    return gross - total_fees


def pnl_pct(pnl: Decimal, notional: Decimal) -> Decimal | None:
    """P&L as % of traded notional. None when notional is not positive."""
    if notional <= 0:
        return None
    return pnl / notional * Decimal("100")


def bucket_cumulative_pnl(
    *,
    capital: Decimal,
    available: Decimal,
    locked: Decimal,
    adjustments: Decimal = Decimal("0"),
) -> tuple[Decimal, Decimal | None]:
    """Cumulative bot P&L for a bucket whose wallet mirrors a sub-account.

    equity = available + locked
    pnl    = equity − (capital + adjustments)

    ``adjustments`` covers manual deposits (+) / withdrawals (−) made on
    the sub-account after the initial capital seed, read from
    ``bucket_state.extra["capital_adjustments_inr"]``. Returns
    (pnl_amount, pnl_pct_vs_capital) — pct is None if the effective
    capital base is not positive.
    """
    base = capital + adjustments
    pnl = (available + locked) - base
    if base <= 0:
        return pnl, None
    return pnl, pnl / base * Decimal("100")
