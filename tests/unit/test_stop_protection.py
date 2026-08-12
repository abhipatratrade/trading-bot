"""Protective stop-loss planner (Decision 022) — pure logic, no I/O."""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import OpenOrder, PositionInfo
from src.brokers.delta_india.client import DeltaIndiaClient
from src.brokers.dhan.client import DhanClient
from src.safety import stop_protection as sp
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


# ── attribution: which bucket owns this position? (2026-08-11) ────────────
# intraday-indian filled CASTROLIND (product INTRADAY) at 09:40:40 and the stop
# sweep ran 9 seconds later. _load_attribution read only the Position table,
# which the reconciler writes every 5 MINUTES, so it was empty. The symbol fell
# through to bucket_ids[0] = swing-indian and the stop went out as MTF. Dhan
# rejected it, and kept rejecting it 116 times over 5.5 hours while a live ₹50k
# position sat with no resting stop.
def _t(sym, bucket, strat="s", extra=None):
    return (sym, bucket, strat, extra or {})


class TestMergeAttribution:
    def test_trade_attributes_before_the_reconciler_has_run(self) -> None:
        """The exact failure: a fill with no Position row yet."""
        got = sp.merge_attribution([_t("CASTROLIND", "intraday-indian")], [])
        assert got["CASTROLIND"] == ("intraday-indian", "s")

    def test_position_wins_once_reconciled(self) -> None:
        """Position reflects the exchange; a Trade only reflects what we asked."""
        got = sp.merge_attribution(
            [_t("X", "swing-indian", "meanrev")],
            [("X", "intraday-indian", "gap")],
        )
        assert got["X"] == ("intraday-indian", "gap")

    def test_newest_entry_wins(self) -> None:
        """Trades arrive newest-first, same convention as _load_stop_distances."""
        got = sp.merge_attribution(
            [_t("X", "intraday-indian", "new"), _t("X", "swing-indian", "old")], []
        )
        assert got["X"] == ("intraday-indian", "new")

    def test_reduce_only_leg_does_not_attribute(self) -> None:
        """An exit would name the strategy that CLOSED the position, not the
        one holding it — and a protective stop is itself reduce-only, so this
        would otherwise let the sweep attribute off its own past orders."""
        got = sp.merge_attribution(
            [
                _t("X", "swing-indian", "exit", {"reduce_only": True}),
                _t("X", "intraday-indian", "entry"),
            ],
            [],
        )
        assert got["X"] == ("intraday-indian", "entry")

    def test_closed_entry_does_not_attribute(self) -> None:
        got = sp.merge_attribution(
            [_t("X", "swing-indian", "old", {"closed_by_trade_id": 42})], []
        )
        assert "X" not in got

    def test_unknown_symbol_is_absent_not_guessed(self) -> None:
        """plan_stop_protection reads .get(sym, (None, None)); absence must stay
        absence so the bucket-percent fallback still applies."""
        assert sp.merge_attribution([], []) == {}

    def test_a_null_bucket_position_row_does_erase_the_trade_attribution(self) -> None:
        """Documents the REAL behaviour, which is the opposite of what an
        earlier version of this test claimed. Positions are applied last and
        unconditionally, so a null-bucket row wins if one ever reaches here.

        Nothing does today: _load_attribution's query filters on
        ``Position.bucket_id.in_(bucket_ids)``, and SQL ``IN`` never matches
        NULL, so the reconciler's orphan imports are excluded upstream. Pinned
        because relaxing that filter — a plausible follow-up for the shared
        account — would silently reintroduce the 2026-08-11 mis-attribution.
        """
        got = sp.merge_attribution(
            [_t("X", "intraday-indian")], [("X", None, None)]
        )
        assert got["X"] == (None, None), (
            "if this ever fails, merge_attribution grew a guard and "
            "_load_attribution's Position filter may safely be widened"
        )


# ── the three defects the 2026-08-12 adversarial review found ────────────
def test_attribution_query_includes_pending() -> None:
    """A Dhan order is PENDING the instant it is placed, and stays PENDING for
    minutes. CASTROLIND still read PENDING a full minute after its fill, and
    the sweep runs ~9s after. The first draft of this fix filtered on
    FILLED/PARTIAL/OPEN and so could never see the row it existed to find.
    """
    import inspect

    src = inspect.getsource(sp._load_attribution)
    assert "OrderStatus.PENDING" in src


def test_stop_carries_the_buckets_product() -> None:
    """Attribution is INERT without this. place_order omitted `product`, so
    OrderRequest.product was None and DhanClient fell back to its MTF default —
    every stop went out as MTF whatever bucket held the position.
    """
    import inspect

    src = inspect.getsource(sp.ensure_stop_protection)
    assert "product=" in src
    assert "product_by_bucket" in src


class TestDhanStopRecognition:
    """plan_stop_protection only matches an existing stop when reduce_only is
    True. Dhan hardcoded it False, so the sweep never recognised its own stops
    and re-planned one every 60s — which only became dangerous once MTF consent
    let a stop actually rest.
    """

    @staticmethod
    def _order(trig, corr):
        raw = {
            "orderId": "1", "correlationId": corr, "tradingSymbol": "X",
            "transactionType": "SELL", "quantity": 10, "filledQty": 0,
            "orderType": "STOP_LOSS_MARKET", "orderStatus": "PENDING",
            "triggerPrice": trig, "createTime": "2026-08-12 09:20:00",
        }
        return DhanClient._to_open_order.__wrapped__(None, raw) if hasattr(
            DhanClient._to_open_order, "__wrapped__"
        ) else DhanClient._to_open_order(_DUMMY_CLIENT, raw)

    def test_our_own_resting_stop_is_recognised(self) -> None:
        assert self._order(159.0, "abc123").reduce_only is True

    def test_a_users_manual_stop_is_never_matched(self) -> None:
        """No correlationId ⇒ not ours ⇒ plan_stop_protection must not cancel
        it. This is the Decision 027 property, and it is why the inference is
        keyed on correlationId rather than just 'has a trigger price'."""
        assert self._order(159.0, None).reduce_only is False
        assert self._order(159.0, "").reduce_only is False

    def test_a_plain_order_is_not_a_stop(self) -> None:
        assert self._order(None, "abc123").reduce_only is False
        assert self._order(0, "abc123").reduce_only is False


_DUMMY_CLIENT = DhanClient.__new__(DhanClient)


# ── PIIND, 2026-08-12: swing-indian's first ever fill went unprotected ────
_PIIND = PositionInfo(
    symbol="PIIND", side="long", size=Decimal("15"), entry_price=Decimal("2514.50")
)


def _piind_plan(distance=None, band=None):
    return plan_stop_protection(
        positions=[_PIIND],
        open_orders=[],
        stop_pct_by_bucket={"swing-indian": Decimal("20")},
        attribution={"PIIND": ("swing-indian", "mean_reversion_1h")},
        tick_sizes={"PIIND": Decimal("0.05")},
        stop_distances=({"PIIND": distance} if distance else None),
        price_band_pct=({"PIIND": band} if band else None),
    )


def test_stop_distance_query_includes_pending() -> None:
    """The strategy DID emit its ATR stop (200.089). The sweep ran 14s after the
    fill, the entry Trade was still PENDING, this query filtered it out, and the
    sweep fell back to the bucket's 20% — which is unplaceable. Same filter bug
    I fixed in _load_attribution the night before and missed here.
    """
    import inspect

    assert "OrderStatus.PENDING" in inspect.getsource(sp._load_stop_distances)


def test_unclamped_bucket_stop_is_outside_the_band() -> None:
    """Documents what actually shipped: -20%, which PIIND's 10% circuit band
    makes impossible to place, so the position got NO stop at all."""
    assert _piind_plan().place[0].trigger == Decimal("2011.60")


def test_band_clamp_makes_the_fallback_placeable() -> None:
    """A tighter stop that EXISTS beats a correctly-sized one that does not."""
    trig = _piind_plan(band=Decimal("10")).place[0].trigger
    pct = (trig / _PIIND.entry_price - 1) * 100
    assert trig == Decimal("2288.20")
    assert Decimal("-10") < pct < Decimal("0"), "must land inside the 10% band"


def test_strategy_atr_stop_needs_no_clamp() -> None:
    """The intended stop was always inside the band — the clamp is only a net
    for when the strategy distance is missing."""
    trig = _piind_plan(distance=Decimal("200.089"), band=Decimal("10")).place[0].trigger
    assert trig == Decimal("2314.40")


def test_clamp_only_ever_tightens() -> None:
    """It must never widen a stop past what was asked for."""
    tight = _piind_plan(distance=Decimal("50"), band=Decimal("10")).place[0].trigger
    assert tight == Decimal("2464.50")  # untouched by the clamp


def test_no_band_means_no_clamp() -> None:
    """Crypto perps have no circuit band; behaviour must be unchanged there."""
    assert _piind_plan(band=None).place[0].trigger == Decimal("2011.60")


def test_short_position_clamps_upward() -> None:
    short = PositionInfo(
        symbol="PIIND", side="short", size=Decimal("15"),
        entry_price=Decimal("2514.50"),
    )
    p = plan_stop_protection(
        positions=[short], open_orders=[],
        stop_pct_by_bucket={"swing-indian": Decimal("20")},
        attribution={"PIIND": ("swing-indian", "m")},
        tick_sizes={"PIIND": Decimal("0.05")},
        price_band_pct={"PIIND": Decimal("10")},
    )
    trig = p.place[0].trigger
    assert trig == Decimal("2740.80")
    assert trig < _PIIND.entry_price * Decimal("1.20")  # tighter than the raw 20%
