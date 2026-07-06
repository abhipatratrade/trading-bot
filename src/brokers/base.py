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

    def close(self) -> None:  # noqa: B027
        """Release resources.  Default no-op; override if needed."""
