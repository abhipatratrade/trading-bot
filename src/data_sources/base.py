"""
Market data interface — the contract between exchange-specific data
adapters and the scanner / strategy runners.

Return types are plain dataclasses so this module stays importable
from the backtester without live-only dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Interval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


@dataclass(frozen=True, slots=True)
class OHLCVBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last_price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume_24h: Decimal = Decimal("0")
    open_interest: Decimal | None = None
    mark_price: Decimal | None = None
    funding_rate: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FundingRate:
    symbol: str
    rate: Decimal
    next_funding_time: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class MarketData(ABC):
    """Abstract market data source.

    Implementations wrap a single exchange's public REST API.  All methods
    are synchronous.  WebSocket feeds are handled by a separate companion
    class where provided.
    """

    @abstractmethod
    def get_ohlcv(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[OHLCVBar]: ...

    @abstractmethod
    def get_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    def get_tickers(self) -> list[Ticker]: ...

    @abstractmethod
    def get_funding_rate(self, symbol: str) -> FundingRate: ...

    def close(self) -> None:  # noqa: B027
        """Release resources.  Default no-op; override if needed."""
