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


class AttachedStopRetireError(Exception):
    """A closing order was ABANDONED because its attached stop could not go.

    Lives on the broker CONTRACT rather than in one adapter because it says
    something every caller needs to act on regardless of venue: **the closing
    order was never sent**. ``OrderManager`` treats it differently from a
    normal failure for that reason — no recovery lookup (nothing landed) and
    no REJECTED row (nothing was refused).

    Raised by adapters whose venue keeps a protective stop attached to the
    entry (Decision 034), when that stop cannot be retired before the position
    is closed. Letting the close proceed would leave the stop resting against a
    position that no longer exists.
    """


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
    # Broker margin/product mode, when the venue has one. Dhan cash equity
    # needs it per ORDER, not per client: swing-indian trades MTF (funded
    # delivery) while intraday-indian trades INTRADAY (MIS), and both share
    # one Dhan account and therefore one client (Decision 029). None ⇒ the
    # broker adapter's configured default. Delta India ignores it.
    product: str | None = None
    # Largest size affordable UNLEVERAGED (1x), for adapters that retry a
    # rejected leveraged order on a cash product (Decision 029 amended
    # 2026-07-27: an MIS-ineligible scrip falls back to 1x CNC).
    #
    # ``size`` is sized for the LEVERAGED product, so re-sending it verbatim
    # on a 1x product would demand `leverage`× the margin the sizer budgeted —
    # a ₹10k slot silently consuming ₹40k of cash. The fallback must clamp to
    # this. None ⇒ no fallback; the rejection propagates.
    fallback_max_size: Decimal | None = None
    # Protective stop to be placed ATOMICALLY WITH THIS ENTRY, by venues that
    # support it (Dhan Super Order — Decision 034). Distinct from
    # ``stop_price``, which makes the order ITSELF a standalone stop.
    #
    # The difference is the whole point of Decision 034: a separate stop is
    # placed AFTER the entry fills, so a stop the venue refuses leaves a naked
    # position. An attached stop is part of the same request, so a refused stop
    # means the ENTRY never happens. Fail-safe instead of fail-open.
    #
    # Never set this without checking ``Broker.supports_attached_stop()`` —
    # ``OrderManager.place_order`` raises rather than let an adapter that
    # cannot honour it place an unprotected entry.
    attached_stop_price: Decimal | None = None
    # Take-profit for the same atomic order. Dhan REQUIRES a target leg on a
    # super order, but no strategy here has a target, so this exists to be
    # satisfied and then retired — see ``DhanClient.place_order``. Callers that
    # genuinely want no target leave it None and let the adapter choose a value
    # it can immediately cancel.
    attached_target_price: Decimal | None = None


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

    def required_margin(  # noqa: B027
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        product: str | None = None,
    ) -> Decimal | None:
        """Margin the venue will actually demand for this order, or None.

        Exists because a bucket's ``leverage_max`` is an *intent*, not a
        promise: Dhan grants intraday leverage per scrip, and a name that
        only gets 2x would have its 5x-sized order rejected by RMS at
        placement (Decision 029). Callers size against the returned figure
        instead of assuming the bucket's leverage was granted.

        None ⇒ the venue offers no preflight, and the caller must fall back
        to something it can guarantee (see ``BucketRunner._fit_to_margin``).
        Default implementation is None so venues without the concept — and
        the crypto path, where leverage IS set explicitly per position —
        are unaffected.
        """
        return None

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

    def supports_attached_stop(self) -> bool:  # noqa: B027
        """True when this venue can carry a protective stop ON the entry order.

        False (the default, and Delta India) means protection has to be a
        SEPARATE order placed after the fill — the Decision 022 sweep, with the
        window between fill and stop that Decision 034 exists to close.

        Callers must consult this before setting ``OrderRequest``'s
        ``attached_stop_price``; ``OrderManager.place_order`` refuses to send an
        attached stop to an adapter that answers False, because an adapter that
        silently dropped it would place exactly the unprotected entry the
        attachment was meant to prevent.
        """
        return False

    def attached_stop_triggers(self) -> dict[str, Decimal]:  # noqa: B027
        """``symbol → trigger`` for stops the venue holds against our entries.

        Only meaningful when :meth:`supports_attached_stop` is True. The
        Decision 022 sweep reads this to know a position is ALREADY protected,
        so it neither stacks a second resting stop on top nor treats the
        venue's own leg as an orphan to cancel.

        Empty dict ⇒ nothing attached (or the venue has no such concept), which
        is the pre-Decision-034 behaviour.
        """
        return {}

    def retire_attached_stop(self, symbol: str) -> None:  # noqa: B027
        """Cancel the venue-side stop attached to ``symbol``'s entry.

        Called on two paths: before the bot closes a position (so the stop
        cannot outlive it), and by the sweep when a leg is found with no
        position behind it at all.

        Must raise :class:`AttachedStopRetireError` if a stop exists and cannot
        be retired — callers rely on that to abandon a close rather than risk
        selling twice. No-op by default, for venues with nothing attached.
        """

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
