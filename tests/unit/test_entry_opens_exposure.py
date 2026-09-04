"""An entry is "not reduce_only", not "a BUY".

The stop sweep's orphan-leg pass computes::

    retire_legs = set(attached_stops) - bot_held - recent_entries

``recent_entries`` exists solely to stop it cancelling the protection of a
position the broker has not surfaced yet. It queried ``Trade.side == BUY``, so
a SHORT entry could never appear in it — and on 2026-09-04 both subtrahends
came back empty for one position:

    18:22:46  commodity-indian opens SHORT 1 NATGASMINI, stop leg at 292.60
    18:23:00  orphan_attached_leg_retired  symbol=NATGASMINI-20260925-FUT

Fourteen seconds. The position then ran naked, and the app confirmed it: Super
orders "Active 1" (KEI only), both NATGASMINI orders under "Cancel".

``bot_held`` was fixed in 03013f3. This is the second line of defence, which
would have caught it independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.core.models import OrderSide, OrderStatus
from src.order_manager.ownership import opens_exposure


@dataclass
class _T:
    side: OrderSide
    symbol: str = "NATGASMINI-20260925-FUT"
    quantity: Decimal = Decimal("1")
    status: OrderStatus = OrderStatus.FILLED
    extra: dict = field(default_factory=dict)


# ── the live regression ─────────────────────────────────────────────────


def test_a_short_entry_opens_exposure() -> None:
    """The 18:22 NATGASMINI sell-to-open. Invisible under `side == BUY`."""
    assert opens_exposure(_T(OrderSide.SELL, extra={"reduce_only": False}))


def test_a_long_entry_opens_exposure() -> None:
    assert opens_exposure(_T(OrderSide.BUY, extra={"reduce_only": False}))


# ── exits must NOT count as entries ─────────────────────────────────────


def test_a_sell_exit_does_not() -> None:
    """Counting an exit would keep a genuinely orphaned leg alive forever."""
    assert not opens_exposure(_T(OrderSide.SELL, extra={"reduce_only": True}))


def test_a_buy_to_cover_does_not() -> None:
    """Closing a short is a BUY — the mirror trap. Under the old `side == BUY`
    rule this counted as an entry, which is the same bug pointing the other
    way: a leg kept alive for a position that had just been closed."""
    assert not opens_exposure(_T(OrderSide.BUY, extra={"reduce_only": True}))


def test_a_protective_stop_does_not() -> None:
    """The sweep stamps reduce_only on every stop it places."""
    assert not opens_exposure(
        _T(OrderSide.BUY, extra={"reduce_only": True, "protective_stop": True})
    )


# ── historic rows keep the pre-036 reading ──────────────────────────────


def test_a_row_with_no_flag_falls_back_to_the_long_only_rule() -> None:
    """Every trade written before reduce_only was stamped. The fallback keeps
    those resolving exactly as `side == BUY` did, so this change cannot
    re-classify history."""
    assert opens_exposure(_T(OrderSide.BUY, extra={}))
    assert not opens_exposure(_T(OrderSide.SELL, extra={}))


def test_a_row_with_no_extra_at_all_is_safe() -> None:
    """`extra` is nullable on Trade."""

    @dataclass
    class _Bare:
        side: OrderSide
        symbol: str = "X"
        quantity: Decimal = Decimal("1")
        status: OrderStatus = OrderStatus.FILLED

    assert opens_exposure(_Bare(OrderSide.BUY))
    assert not opens_exposure(_Bare(OrderSide.SELL))
