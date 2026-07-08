"""
Broker interface — the contract between exchange adapters and the
order manager / strategy runners.

Return types are plain dataclasses (not ORM models) so the broker layer
stays importable from the backtester without a live DB.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Broker-agnostic order intent.  Built by the order manager."""

    symbol: str
    side: str  # "buy" | "sell"
    size: Decimal
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    client_order_id: str | None = None
    # When set, the order is a stop order resting at this trigger price
    # (stop-market when order_type is MARKET). Used by the protective
    # stop-loss layer (Decision 022).
    stop_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    """What the broker hands back after placing an order."""

    exchange_order_id: str
    client_order_id: str | None
    symbol: str
    side: str
    size: Decimal
    price: Decimal | None
    status: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CancelResult:
    exchange_order_id: str
    symbol: str
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PositionInfo:
    symbol: str
    side: str  # "long" | "short" | "flat"
    size: Decimal
    entry_price: Decimal
    liquidation_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    leverage: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BalanceInfo:
    asset: str
    available: Decimal
    order_margin: Decimal
    position_margin: Decimal
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FillInfo:
    """One execution (fill) reported by the exchange."""

    fill_id: str
    exchange_order_id: str
    symbol: str
    side: str  # "buy" | "sell"
    size: Decimal
    price: Decimal
    commission: Decimal
    timestamp: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OpenOrder:
    exchange_order_id: str
    client_order_id: str | None
    symbol: str
    side: str
    size: Decimal
    unfilled_size: Decimal
    order_type: str
    limit_price: Decimal | None
    status: str
    stop_price: Decimal | None = None
    reduce_only: bool = False
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class Broker(ABC):
    """Abstract broker interface.

    Implementations wrap a single exchange's REST API.  All methods are
    synchronous (matching the sync DB layer).  WebSocket feeds are handled
    by a separate companion class.
    """

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def cancel_order(
        self,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
        symbol: str,
    ) -> CancelResult: ...

    @abstractmethod
    def get_positions(self) -> list[PositionInfo]: ...

    @abstractmethod
    def get_balances(self) -> list[BalanceInfo]: ...

    @abstractmethod
    def get_open_orders(self, symbol: str | None = None) -> list[OpenOrder]: ...

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: Decimal) -> None: ...

    def get_order(self, exchange_order_id: str) -> OpenOrder | None:  # noqa: B027
        """Fetch a single order by exchange ID.  Returns None if not found."""
        return None

    def get_order_by_client_id(self, client_order_id: str) -> OpenOrder | None:  # noqa: B027
        """Fetch a single order by client_order_id.  Returns None if not found.

        Used by the order manager to recover from transport errors without
        double-firing (an order may land on the exchange even when the HTTP
        response never made it back).
        """
        return None

    def get_fills(
        self, *, start_time: datetime | None = None
    ) -> list[FillInfo]:  # noqa: B027
        """Fills since ``start_time`` (exchange-defined ordering).

        Default: empty — brokers without a fills API just skip P&L
        enrichment. Used by the reconciler to record avg fill price,
        commissions, and realized P&L per trade.
        """
        return []

    def contract_size(
        self, symbol: str, default: Decimal | None = Decimal("1")
    ) -> Decimal | None:  # noqa: B027
        """Units of base asset per contract, from the venue's product spec.

        Returns ``default`` when the venue doesn't know the symbol (or the
        catalogue fetch fails). Display/P&L callers keep the historical
        ``Decimal("1")`` default; the sizer passes ``default=None`` so an
        unknown live value falls back to the YAML table instead of
        silently sizing at 1.
        """
        return default

    def tick_size(self, symbol: str) -> Decimal | None:  # noqa: B027
        """Price increment from the venue's product spec; None when unknown.

        Used to snap stop-trigger prices onto the venue's grid before
        placement.
        """
        return None

    def wallet_flow_totals(self) -> tuple[Decimal, Decimal] | None:  # noqa: B027
        """(total_deposited, total_withdrawn) in the account's settlement
        currency, summed over the venue's wallet-transaction history.

        Counts external deposits/withdrawals and sub-account transfers;
        trading cashflows (pnl, fees, funding) are excluded. None when the
        venue doesn't expose a transaction history.
        """
        return None

    def close(self) -> None:  # noqa: B027
        """Release resources.  Default no-op; override if needed."""
