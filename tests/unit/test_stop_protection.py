"""Protective stop-loss planner (Decision 022) — pure logic, no I/O."""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import OpenOrder, PositionInfo
from src.brokers.delta_india.client import DeltaIndiaClient
from src.safety.stop_protection import (
    expected_trigger,
    plan_stop_protection,
)

PCTS = {"longterm-crypto": Decimal("10")}
ATTR = {"BTCUSD": ("longterm-crypto", "top5_volume")}


def _pos(
    symbol: str = "BTCUSD",
    side: str = "long",
    size: str = "10",
    entry: str = "50000",
) -> PositionInfo:
    return PositionInfo(
        symbol=symbol,
        side=side,
        size=Decimal(size),
        entry_price=Decimal(entry),
    )


def _stop(
    symbol: str = "BTCUSD",
    side: str = "sell",
    size: str = "10",
    trigger: str = "45000",
    oid: str = "1",
    reduce_only: bool = True,
) -> OpenOrder:
    return OpenOrder(
        exchange_order_id=oid,
        client_order_id=None,
        symbol=symbol,
        side=side,
        size=Decimal(size),
        unfilled_size=Decimal(size),
        order_type="market_order",
        limit_price=None,
        status="pending",
        stop_price=Decimal(trigger),
        reduce_only=reduce_only,
    )


# ── expected_trigger ────────────────────────────────────────────────────


def test_long_trigger_below_entry() -> None:
    t = expected_trigger(Decimal("50000"), "long", Decimal("10"))
    assert t == Decimal("45000")


def test_short_trigger_above_entry() -> None:
    t = expected_trigger(Decimal("50000"), "short", Decimal("10"))
    assert t == Decimal("55000")


def test_trigger_snaps_to_tick() -> None:
    # 101 × 0.9 = 90.9 → nearest 0.5 tick = 91.0
    t = expected_trigger(Decimal("101"), "long", Decimal("10"), Decimal("0.5"))
    assert t == Decimal("91.0")


# ── plan_stop_protection ────────────────────────────────────────────────


def test_unprotected_long_gets_sell_stop() -> None:
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert len(plan.place) == 1
    stop = plan.place[0]
    assert (stop.side, stop.size, stop.trigger) == (
        "sell",
        Decimal("10"),
        Decimal("45000"),
    )
    assert stop.bucket_id == "longterm-crypto"
    assert not plan.cancel


def test_unprotected_short_gets_buy_stop() -> None:
    plan = plan_stop_protection(
        positions=[_pos(side="short")],
        open_orders=[],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert plan.place[0].side == "buy"
    assert plan.place[0].trigger == Decimal("55000")


def test_matching_stop_is_noop() -> None:
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[_stop()],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert not plan.place
    assert not plan.cancel


def test_small_trigger_drift_is_tolerated() -> None:
    # 0.2% away from expected 45000 — within the 0.5% tolerance.
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[_stop(trigger="44910")],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert not plan.place
    assert not plan.cancel


def test_size_mismatch_replaces_stop() -> None:
    # Position grew (add) but the stop still covers the old size.
    plan = plan_stop_protection(
        positions=[_pos(size="15")],
        open_orders=[_stop(size="10")],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert len(plan.cancel) == 1
    assert len(plan.place) == 1
    assert plan.place[0].size == Decimal("15")


def test_trigger_drift_replaces_stop() -> None:
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[_stop(trigger="40000")],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert len(plan.cancel) == 1
    assert plan.place[0].trigger == Decimal("45000")


def test_orphan_stop_is_canceled() -> None:
    plan = plan_stop_protection(
        positions=[],
        open_orders=[_stop()],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert not plan.place
    assert len(plan.cancel) == 1


def test_duplicate_stops_keep_one_cancel_rest() -> None:
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[_stop(oid="1"), _stop(oid="2")],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert not plan.place
    assert [o.exchange_order_id for o in plan.cancel] == ["2"]


def test_non_reduce_only_stop_is_not_protective() -> None:
    # A non-reduce-only stop doesn't count as protection and is left alone.
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[_stop(reduce_only=False)],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
    )
    assert len(plan.place) == 1
    assert not plan.cancel


def test_unattributed_symbol_uses_most_conservative_pct() -> None:
    pcts = {"a": Decimal("10"), "b": Decimal("5")}
    plan = plan_stop_protection(
        positions=[_pos(symbol="ETHUSD", entry="2000")],
        open_orders=[],
        stop_pct_by_bucket=pcts,
        attribution={},
    )
    assert plan.place[0].trigger == Decimal("1900")  # 5% (min), not 10%


def test_no_pct_configured_marks_unprotectable() -> None:
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[_stop()],  # stale stop stays — better than none
        stop_pct_by_bucket={},
        attribution=ATTR,
    )
    assert not plan.place
    assert not plan.cancel
    assert plan.unprotectable == ["BTCUSD"]


# ── Delta client mapping ────────────────────────────────────────────────


def test_delta_parses_stop_fields_from_order() -> None:
    order = DeltaIndiaClient._to_open_order(
        {
            "id": 7,
            "side": "sell",
            "size": 10,
            "unfilled_size": 10,
            "order_type": "market_order",
            "state": "pending",
            "product_symbol": "BTCUSD",
            "stop_price": "45000",
            "reduce_only": True,
        }
    )
    assert order.stop_price == Decimal("45000")
    assert order.reduce_only is True
    assert order.status == "pending"


def test_delta_stop_order_body() -> None:
    from src.brokers.base import OrderRequest, OrderType
    from src.core.logging import get_logger

    client = DeltaIndiaClient.__new__(DeltaIndiaClient)
    client._log = get_logger("test")
    captured: dict[str, object] = {}

    def _fake_post(path: str, body: dict[str, object]) -> dict[str, object]:
        captured.update(body)
        return {"id": 1, "side": "sell", "size": 10, "state": "pending"}

    client._post = _fake_post  # type: ignore[method-assign]
    client.place_order(
        OrderRequest(
            symbol="BTCUSD",
            side="sell",
            size=Decimal("10"),
            order_type=OrderType.MARKET,
            reduce_only=True,
            stop_price=Decimal("45000"),
            client_order_id="abc",
        )
    )
    assert captured["stop_order_type"] == "stop_loss_order"
    assert captured["stop_price"] == "45000"
    assert captured["stop_trigger_method"] == "mark_price"
    assert captured["reduce_only"] == "true"
    assert captured["order_type"] == "market_order"


# ── Shared-account scoping (Decision 027 followup, 2026-07-22 incident) ──
_DHAN_PCTS = {"intraday-indian": Decimal("15")}
_DHAN_ATTR = {"RELIANCE": ("intraday-indian", "gap_down_reversal")}


def _eqpos(symbol: str, size: str = "100", entry: str = "1000") -> PositionInfo:
    return PositionInfo(
        symbol=symbol, side="long", size=Decimal(size), entry_price=Decimal(entry)
    )


def test_shared_skips_user_position_entirely() -> None:
    """A position the bot doesn't own gets NO stop (the 2026-07-22 bug)."""
    plan = plan_stop_protection(
        positions=[_eqpos("NIFTY-OPT-CE")],   # user's manual position
        open_orders=[],
        stop_pct_by_bucket=_DHAN_PCTS,
        attribution={},                        # not attributed to any bucket
        owned_quantities={},                   # bot owns nothing
    )
    assert plan.place == []
    assert plan.cancel == []
    assert plan.unprotectable == []


def test_shared_protects_only_bot_position() -> None:
    plan = plan_stop_protection(
        positions=[_eqpos("RELIANCE"), _eqpos("NIFTY-OPT-CE")],
        open_orders=[],
        stop_pct_by_bucket=_DHAN_PCTS,
        attribution=_DHAN_ATTR,
        owned_quantities={"RELIANCE": Decimal("100")},
    )
    assert [p.symbol for p in plan.place] == ["RELIANCE"]
    assert plan.place[0].size == Decimal("100")


def test_shared_never_cancels_user_resting_stop() -> None:
    """The user's own resting stop on an unowned symbol must be left alone."""
    user_stop = _stop(symbol="NIFTY-OPT-CE", side="sell", size="75", oid="u1")
    plan = plan_stop_protection(
        positions=[_eqpos("NIFTY-OPT-CE", size="75")],
        open_orders=[user_stop],
        stop_pct_by_bucket=_DHAN_PCTS,
        attribution={},
        owned_quantities={},
    )
    assert plan.cancel == [], "must not cancel the user's stop"
    assert plan.place == []


def test_shared_caps_stop_size_to_bot_quantity() -> None:
    """Bot holds 100, exchange shows 150 (user also long 50) → stop only 100."""
    plan = plan_stop_protection(
        positions=[_eqpos("RELIANCE", size="150")],
        open_orders=[],
        stop_pct_by_bucket=_DHAN_PCTS,
        attribution=_DHAN_ATTR,
        owned_quantities={"RELIANCE": Decimal("100")},
    )
    assert len(plan.place) == 1
    assert plan.place[0].size == Decimal("100"), "never cover the user's 50"


def test_exclusive_account_unchanged_without_owned() -> None:
    """owned_quantities=None (crypto) → every position protected, as before."""
    plan = plan_stop_protection(
        positions=[_pos()],
        open_orders=[],
        stop_pct_by_bucket=PCTS,
        attribution=ATTR,
        owned_quantities=None,
    )
    assert [p.symbol for p in plan.place] == ["BTCUSD"]
