"""Bot ownership on shared accounts (Decision 027 followup).

The rule that keeps the bot off the user's manual positions: a symbol is
bot-owned only if the bot's OWN trades net to a positive long quantity.

Tests exercise the PURE ``net_owned`` with lightweight trade stand-ins — no
database (the models use Postgres JSONB, so the suite keeps DB out of unit
tests; the thin ``bot_owned_quantities`` query wrapper is covered by the VM
soak instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.core.models import OrderSide, OrderStatus
from src.order_manager.ownership import net_owned


@dataclass
class _T:
    symbol: str
    side: OrderSide
    quantity: Decimal
    status: OrderStatus = OrderStatus.FILLED


def _buy(sym: str, qty: str, status: OrderStatus = OrderStatus.FILLED) -> _T:
    return _T(sym, OrderSide.BUY, Decimal(qty), status)


def _sell(sym: str, qty: str, status: OrderStatus = OrderStatus.FILLED) -> _T:
    return _T(sym, OrderSide.SELL, Decimal(qty), status)


def test_open_entry_is_owned() -> None:
    assert net_owned([_buy("RELIANCE", "100")]) == {"RELIANCE": Decimal("100")}


def test_fully_exited_is_not_owned() -> None:
    assert net_owned([_buy("RELIANCE", "100"), _sell("RELIANCE", "100")]) == {}


def test_partial_exit_leaves_residual_owned() -> None:
    assert net_owned([_buy("TCS", "50"), _sell("TCS", "20")]) == {
        "TCS": Decimal("30")
    }


def test_user_position_never_appears() -> None:
    """No bot trade for a symbol → never owned (the 2026-07-22 bug)."""
    owned = net_owned([_buy("RELIANCE", "100")])
    assert "NIFTY-Jul2026-24450-CE" not in owned
    assert set(owned) == {"RELIANCE"}


def test_pending_entry_is_owned_before_fill() -> None:
    """A just-placed entry counts, so the first reconcile recognises it."""
    assert net_owned([_buy("INFY", "40", OrderStatus.PENDING)]) == {
        "INFY": Decimal("40")
    }


def test_rejected_entry_is_not_owned() -> None:
    assert net_owned([_buy("INFY", "40", OrderStatus.REJECTED)]) == {}


def test_canceled_entry_is_not_owned() -> None:
    assert net_owned([_buy("INFY", "40", OrderStatus.CANCELED)]) == {}


def test_pending_exit_does_not_reduce_ownership() -> None:
    """A not-yet-filled exit must not make the bot abandon a held position."""
    trades = [_buy("ITC", "60"), _sell("ITC", "60", OrderStatus.PENDING)]
    assert net_owned(trades) == {"ITC": Decimal("60")}


def test_filled_exit_reduces_pending_exit_ignored() -> None:
    trades = [
        _buy("SBIN", "100"),
        _sell("SBIN", "40"),  # filled → subtract
        _sell("SBIN", "60", OrderStatus.PENDING),  # pending → ignored
    ]
    assert net_owned(trades) == {"SBIN": Decimal("60")}


def test_multiple_symbols() -> None:
    trades = [
        _buy("A", "10"),
        _buy("B", "20"),
        _sell("B", "20"),  # B squared off
        _buy("C", "5", OrderStatus.OPEN),
    ]
    assert net_owned(trades) == {"A": Decimal("10"), "C": Decimal("5")}


def test_empty() -> None:
    assert net_owned([]) == {}
