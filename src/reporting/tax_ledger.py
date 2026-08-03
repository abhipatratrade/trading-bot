"""
Consolidated transaction ledger of the bot's FILLED trades — accounting shape.

WHAT THIS IS NOT. It is not a tax return, and it is not a complete record of
the account. The bot writes a ``trade`` row only for orders IT placed, so
anything traded by hand is absent — on the shared Dhan account (Decision 027)
that is a large omission, and the ``foreign_positions`` invariant exists
precisely because the bot can see those positions but does not own them. The
complete, charge-itemised record is the BROKER's own tax P&L statement. Treat
this file as a reconciliation aid against that statement, never as a substitute.

Two properties of the source data drive the whole design:

* **Realized P&L is stamped on BOTH legs of a round-trip.** The reconciler
  writes ``extra.pnl_usd`` onto the closing SELL and back onto the opening BUY
  (which also gains ``closed_by_trade_id``). Summing the column across all rows
  therefore double-counts every trade. P&L is attributed to the CLOSING leg
  only here — which is also the accounting convention: a gain is recognised on
  disposal, not on acquisition.

* **Currency is per-bucket and is NOT converted.** Delta India contracts are
  USD-denominated; Indian equity is INR. The 85.0 USD/INR in each allocator.yaml
  is a fixed SIZING convention (Decision 024), chosen so position sizing is
  deterministic — it is not a market rate on any given trade date, and using it
  to convert a P&L figure would fabricate a number that no statement agrees
  with. The ledger carries a ``currency`` column and leaves conversion to
  whoever has the real rates.

Delta quantities are in CONTRACTS, so ``quantity × contract_size`` is the base
asset amount; both are emitted rather than silently collapsing one into the
other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_
from decimal import Decimal, InvalidOperation

from src.core.models import BrokerName, OrderSide, OrderStatus, Trade
from src.shared.market_calendar import IST

# Indian financial year: 1 April → 31 March.
FY_START_MONTH = 4

# Delta India settles in USD; Dhan in INR. Broker is the only reliable
# currency signal on a Trade row — nothing else records it.
_CURRENCY_BY_BROKER = {
    BrokerName.DELTA_INDIA: "USD",
    BrokerName.DHAN: "INR",
}

COLUMNS = (
    "date_ist",
    "time_ist",
    "financial_year",
    "broker",
    "bucket",
    "strategy",
    "symbol",
    "side",
    "leg",
    "quantity",
    "contract_size",
    "base_quantity",
    "fill_price",
    "notional",
    "fees",
    "realized_pnl",
    "currency",
    "exchange_order_id",
    "client_order_id",
)


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def financial_year(day: date_) -> str:
    """Indian FY label for a date, e.g. 2026-05-02 → "FY2026-27". PURE."""
    start = day.year if day.month >= FY_START_MONTH else day.year - 1
    return f"FY{start}-{str(start + 1)[-2:]}"


def fy_bounds(label: str) -> tuple[date_, date_]:
    """"FY2026-27" → (2026-04-01, 2027-03-31). PURE."""
    start_year = int(label.removeprefix("FY").split("-")[0])
    return date_(start_year, FY_START_MONTH, 1), date_(
        start_year + 1, FY_START_MONTH - 1, 31
    )


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One filled order, flattened for accounting. Values stay Decimal."""

    trade: Trade

    @property
    def extra(self) -> dict:
        raw = self.trade.extra
        return raw if isinstance(raw, dict) else {}

    @property
    def is_closing(self) -> bool:
        """Did this fill CLOSE exposure?

        ``reduce_only`` is the reconciler's own marker and is authoritative
        where present. Indian equity strategies are long-only and do not set
        it, so a SELL there is a close by construction.
        """
        if self.extra.get("reduce_only"):
            return True
        return (
            self.trade.broker == BrokerName.DHAN
            and self.trade.side == OrderSide.SELL
        )

    @property
    def fill_price(self) -> Decimal | None:
        """``extra.avg_fill_price`` first — ``Trade.price`` is the INTENDED
        price and is NULL on every live fill in the ledger to date."""
        return _dec(self.extra.get("avg_fill_price")) or self.trade.price

    @property
    def realized(self) -> Decimal | None:
        """Realized P&L, on the CLOSING leg only — see the module docstring."""
        if not self.is_closing:
            return None
        return _dec(self.extra.get("pnl_usd")) or _dec(self.extra.get("realized_pnl"))

    def as_dict(self) -> dict:
        t = self.trade
        when = (t.filled_at or t.submitted_at or t.created_at).astimezone(IST)
        contract_size = _dec(self.extra.get("contract_size"))
        qty = t.quantity or Decimal("0")
        price = self.fill_price
        notional = _dec(self.extra.get("traded_notional_usd"))
        if notional is None and price is not None:
            notional = qty * price * (contract_size or Decimal("1"))
        return {
            "date_ist": when.date().isoformat(),
            "time_ist": when.strftime("%H:%M:%S"),
            "financial_year": financial_year(when.date()),
            "broker": t.broker.value,
            "bucket": t.bucket_id or t.strategy_id,
            "strategy": t.strategy_name or "",
            "symbol": t.symbol,
            "side": t.side.value,
            "leg": "CLOSE" if self.is_closing else "OPEN",
            "quantity": qty,
            "contract_size": contract_size,
            "base_quantity": qty * contract_size if contract_size else qty,
            "fill_price": price,
            "notional": notional,
            "fees": t.fees or Decimal("0"),
            "realized_pnl": self.realized,
            "currency": _CURRENCY_BY_BROKER.get(t.broker, ""),
            "exchange_order_id": t.exchange_order_id or "",
            "client_order_id": t.client_order_id,
        }


def build_ledger(
    trades: list[Trade], *, fy: str | None = None
) -> list[dict]:
    """Filled trades → accounting rows, oldest first. PURE.

    Only ``FILLED`` and ``PARTIAL`` orders appear: a rejected or cancelled
    order never moved money and has no place in a ledger. That matters here —
    6,009 of the 6,060 rows on the live database are rejects from the crypto
    soak, and a ledger that included them would be gibberish.
    """
    rows = []
    for trade in trades:
        if trade.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            continue
        row = LedgerRow(trade).as_dict()
        if fy is not None and row["financial_year"] != fy:
            continue
        rows.append(row)
    return sorted(rows, key=lambda r: (r["date_ist"], r["time_ist"]))


def summarise(rows: list[dict]) -> dict[str, dict[str, Decimal]]:
    """Per-currency totals. PURE.

    Keyed by currency because adding USD to INR would be a lie, and a single
    "total" column is exactly the kind of number that gets copied into a form.
    """
    out: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        bucket = out.setdefault(
            row["currency"] or "?",
            {"fills": Decimal("0"), "fees": Decimal("0"), "realized": Decimal("0")},
        )
        bucket["fills"] += 1
        bucket["fees"] += row["fees"] or Decimal("0")
        bucket["realized"] += row["realized_pnl"] or Decimal("0")
    return out
