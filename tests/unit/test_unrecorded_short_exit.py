"""A short the venue closed must leave the ledger too (Decision 037).

2026-09-23 17:35 IST: a NATGASMINI short's attached stop bought it back at the
venue. No order passed through the bot, and ``_detect_unrecorded_exits`` only
knew how to notice a LONG going missing, so the ledger counted −1 for good.
The next evening the bot opened a real +1 long; the phantom netted it to zero,
the bot filed its own position as the user's, and the orphan pass cancelled
its only stop six minutes after entry.
"""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import PositionInfo
from src.order_manager.reconciler import exit_shortfalls

_GAS = "NATGASMINI-20261027-FUT"


def _pos(symbol: str, side: str, size: str) -> PositionInfo:
    return PositionInfo(
        symbol=symbol, side=side, size=Decimal(size), entry_price=Decimal("100")
    )


def test_a_short_bought_back_at_the_venue_is_a_negative_shortfall() -> None:
    """The live case: ledger −1, account flat."""
    assert exit_shortfalls({_GAS: Decimal("-1")}, []) == {_GAS: Decimal("-1")}


def test_a_short_still_held_is_no_shortfall() -> None:
    assert exit_shortfalls({_GAS: Decimal("-1")}, [_pos(_GAS, "short", "1")]) == {}


def test_a_partly_covered_short_reports_only_the_covered_part() -> None:
    got = exit_shortfalls({_GAS: Decimal("-3")}, [_pos(_GAS, "short", "1")])
    assert got == {_GAS: Decimal("-2")}


def test_a_long_on_the_venue_does_not_hide_a_vanished_short() -> None:
    """Short covered, then the account went long. Only the short side is
    measured against the short ledger; a long is not 'the short, still held'."""
    got = exit_shortfalls({_GAS: Decimal("-1")}, [_pos(_GAS, "long", "1")])
    assert got == {_GAS: Decimal("-1")}


# ── the long half must be exactly the pre-037 rule ──────────────────────


def test_a_long_sold_at_the_venue_is_a_positive_shortfall() -> None:
    assert exit_shortfalls({"LTF": Decimal("174")}, []) == {"LTF": Decimal("174")}


def test_a_settlement_short_does_not_net_against_the_holding() -> None:
    """Selling held stock shows as a SHORT day-position for minutes (Dhan).
    Netting it would read the live holding as already sold."""
    positions = [_pos("PIIND", "long", "15"), _pos("PIIND", "short", "15")]
    assert exit_shortfalls({"PIIND": Decimal("15")}, positions) == {}


def test_a_long_still_held_is_no_shortfall() -> None:
    assert exit_shortfalls({"LTF": Decimal("174")}, [_pos("LTF", "long", "174")]) == {}


def test_the_confirmation_count_works_on_a_signed_shortfall() -> None:
    """Three passes, same as a long — one bad read must never write a BUY."""
    from src.core.models import BrokerName
    from src.order_manager.reconciler import Reconciler

    rec = Reconciler(
        broker=None,  # type: ignore[arg-type]
        broker_name=BrokerName.DHAN,
        bucket_ids=["commodity-indian"],
        shared_account=True,
    )
    gap = exit_shortfalls({_GAS: Decimal("-1")}, [])
    assert rec._confirm_shortfalls(gap) == {}
    assert rec._confirm_shortfalls(gap) == {}
    assert rec._confirm_shortfalls(gap) == {_GAS: Decimal("-1")}


# ── the row it writes ───────────────────────────────────────────────────


def test_a_covered_short_is_booked_as_a_reduce_only_buy(monkeypatch) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from src.core.models import BrokerName, OrderSide, Trade
    from src.order_manager import reconciler as rmod

    short_entry = SimpleNamespace(
        bucket_id="commodity-indian", strategy_name="cci_gas_reversion_15m", extra={}
    )
    added: list = []
    asked: list = []

    class _Session:
        def execute(self, stmt):
            asked.append(str(stmt))
            return SimpleNamespace(
                first=lambda: None,
                scalars=lambda: SimpleNamespace(all=lambda: [short_entry]),
            )

        def add(self, obj):
            added.append(obj)

    @contextmanager
    def _scope():
        yield _Session()

    monkeypatch.setattr(rmod, "session_scope", _scope)
    monkeypatch.setattr(rmod, "send_alert", lambda *_a, **_k: None)
    rec = rmod.Reconciler(
        broker=None,  # type: ignore[arg-type]
        broker_name=BrokerName.DHAN,
        bucket_ids=["commodity-indian"],
        shared_account=True,
    )

    rec._write_unrecorded_exit(
        _GAS, Decimal("1"), [], rmod.ReconcileReport(), side="buy"
    )

    trade = next(o for o in added if isinstance(o, Trade))
    assert trade.side == OrderSide.BUY
    assert trade.quantity == Decimal("1")
    assert trade.bucket_id == "commodity-indian"
    assert trade.extra["reduce_only"] is True
    assert trade.extra["synthetic_exit"] is True
