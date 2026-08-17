"""Dhan Super Order — the entry carries its own stop (Decision 034).

The old design placed the entry, then tried to rest a protective stop ~60s
later. Everything here exists because that gap produced four live bugs in one
week, ending with swing-indian's first ever fill (PIIND, 2026-08-12) sitting
unprotected while the venue refused its stop 117 times.

A super order closes the gap by construction: the stop is in the SAME request,
so a stop the venue will not accept means the ENTRY does not happen.

These tests carry unusual weight. There is no usable Dhan sandbox for this
endpoint, so the first real super order is also its first execution — they are
the only rehearsal this code gets before real money.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.brokers.base import (
    AttachedStopRetireError,
    OrderRequest,
    OrderType,
    PositionInfo,
)
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanAPIError, DhanClient
from src.safety.session_invariants import Severity, check_stop_coverage
from src.safety.stop_protection import (
    plan_stop_protection,
    resolve_stop_trigger,
    resolve_target_price,
)

_UNIVERSE = {"PIIND": ("1001", "NSE_EQ"), "TBZ": ("2002", "BSE_EQ")}


def _resolve(symbol: str) -> tuple[str, str]:
    return _UNIVERSE[symbol]


class _Resp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self) -> object:
        return self._payload


class _FakeHttp:
    """Routes ``"<METHOD> <suffix>"`` to queued responses; records every call."""

    def __init__(self, routes: dict[str, list[_Resp]]) -> None:
        self._routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[dict] = []

    def request(self, method, url, json=None, headers=None):  # noqa: A002
        self.calls.append({"method": method, "url": url, "json": json})
        for key, resps in self._routes.items():
            m, suffix = key.split(" ", 1)
            if method == m and url.endswith(suffix) and resps:
                return resps.pop(0)
        raise AssertionError(f"unexpected {method} {url}")

    def paths(self, method: str | None = None) -> list[str]:
        return [
            c["url"].split(".co", 1)[-1]
            for c in self.calls
            if method is None or c["method"] == method
        ]


def _client(http: _FakeHttp, *, product: str = "MTF", owns=None) -> DhanClient:
    return DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=_resolve,
        base_url="https://sandbox.dhan.co",
        product_type=product,
        http=http,
        owns_order_id=owns,
    )


def _entry(**kw) -> OrderRequest:
    base = dict(
        symbol="PIIND",
        side="buy",
        size=Decimal("15"),
        order_type=OrderType.MARKET,
        client_order_id="cid-1",
        attached_stop_price=Decimal("2314.40"),
        attached_target_price=Decimal("2740.00"),
    )
    base.update(kw)
    return OrderRequest(**base)  # type: ignore[arg-type]


def _super_order(
    *,
    order_id: str = "900",
    symbol: str = "PIIND",
    correlation: str = "cid-1",
    stop_status: str = "PENDING",
    target_status: str | None = None,
    stop_price: float = 2314.40,
) -> dict:
    legs = [{"legName": "STOP_LOSS_LEG", "orderStatus": stop_status,
             "stopLossPrice": stop_price}]
    if target_status is not None:
        legs.append({"legName": "TARGET_LEG", "orderStatus": target_status})
    return {
        "orderId": order_id,
        "tradingSymbol": symbol,
        "correlationId": correlation,
        "legDetails": legs,
    }


# ── Placement ────────────────────────────────────────────────────────────
class TestSuperOrderPlacement:
    def test_entry_with_attached_stop_goes_to_super_endpoint(self) -> None:
        http = _FakeHttp({
            "POST /v2/super/orders": [_Resp({"orderId": "900", "orderStatus": "TRANSIT"})],
            "DELETE /v2/super/orders/900/TARGET_LEG": [_Resp({"orderStatus": "CANCELLED"})],
        })
        res = _client(http).place_order(_entry())

        body = http.calls[0]["json"]
        assert http.paths("POST") == ["/v2/super/orders"]
        assert body["securityId"] == "1001"
        assert body["transactionType"] == "BUY"
        assert body["productType"] == "MTF"
        assert body["orderType"] == "MARKET"
        assert body["quantity"] == 15
        assert body["stopLossPrice"] == 2314.40
        assert body["targetPrice"] == 2740.00
        assert body["correlationId"] == "cid-1"
        # Explicitly disabled: our stops are fixed at entry (Decision 032).
        assert body["trailingJump"] == 0
        assert res.exchange_order_id == "900"
        assert res.raw["_super_order"] is True

    def test_super_body_omits_plain_order_only_fields(self) -> None:
        """Dhan's super spec does not list these; this integration has been
        burned repeatedly by sending fields the venue did not ask for."""
        http = _FakeHttp({
            "POST /v2/super/orders": [_Resp({"orderId": "900", "orderStatus": "TRANSIT"})],
            "DELETE /v2/super/orders/900/TARGET_LEG": [_Resp({})],
        })
        _client(http).place_order(_entry())
        body = http.calls[0]["json"]
        for absent in ("validity", "disclosedQuantity", "afterMarketOrder", "triggerPrice"):
            assert absent not in body

    def test_missing_target_is_refused_not_invented(self) -> None:
        """A target outside the circuit band would reject the WHOLE order, so
        the adapter refuses to guess one — the band is the caller's knowledge."""
        http = _FakeHttp({})
        with pytest.raises(ValueError, match="attached_target_price"):
            _client(http).place_order(_entry(attached_target_price=None))
        assert http.calls == []

    def test_plain_entry_still_uses_plain_endpoint(self) -> None:
        http = _FakeHttp({"POST /v2/orders": [_Resp({"orderId": "1", "orderStatus": "TRANSIT"})]})
        _client(http).place_order(
            OrderRequest(symbol="PIIND", side="buy", size=Decimal("1"),
                         order_type=OrderType.MARKET)
        )
        assert http.paths("POST") == ["/v2/orders"]

    def test_cnc_fallback_stays_on_the_super_endpoint(self) -> None:
        """An MTF-ineligible scrip retries as CNC — and MUST keep its stop.
        Retrying as a plain order would place the bare entry this decision
        exists to prevent."""
        http = _FakeHttp({
            "POST /v2/super/orders": [
                _Resp({"errorCode": "DH-905", "errorMessage": "MTF not allowed"}),
                _Resp({"orderId": "901", "orderStatus": "TRANSIT"}),
            ],
            "DELETE /v2/super/orders/901/TARGET_LEG": [_Resp({})],
        })
        res = _client(http).place_order(
            _entry(fallback_max_size=Decimal("4"))
        )
        assert http.paths("POST") == ["/v2/super/orders", "/v2/super/orders"]
        retry = http.calls[1]["json"]
        assert retry["productType"] == "CNC"
        assert retry["stopLossPrice"] == 2314.40   # protection survived
        assert retry["quantity"] == 4              # clamped to 1x affordability
        assert res.size == Decimal("4")


# ── The mandatory target leg ─────────────────────────────────────────────
class TestTargetLeg:
    def test_target_leg_cancelled_immediately_after_entry(self) -> None:
        http = _FakeHttp({
            "POST /v2/super/orders": [_Resp({"orderId": "900", "orderStatus": "TRANSIT"})],
            "DELETE /v2/super/orders/900/TARGET_LEG": [_Resp({"orderStatus": "CANCELLED"})],
        })
        res = _client(http).place_order(_entry())
        assert http.calls[1]["method"] == "DELETE"
        assert http.paths("DELETE") == ["/v2/super/orders/900/TARGET_LEG"]
        assert res.raw["_target_leg_cancelled"] is True

    def test_failed_target_cancel_does_not_lose_the_entry(self) -> None:
        """The entry ALREADY LANDED. Raising here would send OrderManager down
        its transport-error recovery path for an accepted order; the failure is
        recorded and retried instead."""
        http = _FakeHttp({
            "POST /v2/super/orders": [_Resp({"orderId": "900", "orderStatus": "TRANSIT"})],
            "DELETE /v2/super/orders/900/TARGET_LEG": [
                _Resp({"errorCode": "DH-101", "errorMessage": "nope"})
            ],
        })
        res = _client(http).place_order(_entry())
        assert res.exchange_order_id == "900"
        assert res.raw["_target_leg_cancelled"] is False

    def test_stale_target_legs_are_retried_later(self) -> None:
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp([_super_order(target_status="PENDING")])],
            "DELETE /v2/super/orders/900/TARGET_LEG": [_Resp({"orderStatus": "CANCELLED"})],
        })
        assert _client(http).retire_stale_target_legs() == ["900"]

    def test_already_cancelled_target_is_left_alone(self) -> None:
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp([_super_order(target_status="CANCELLED")])],
        })
        assert _client(http).retire_stale_target_legs() == []
        assert http.paths("DELETE") == []


# ── The naked-short guard ────────────────────────────────────────────────
class TestRetireStopBeforeClosing:
    """A stop leg that outlives its position sells stock we no longer hold —
    on MTF that OPENS A SHORT. Strictly worse than the missing stop this whole
    decision exists to fix, so the ordering here is not negotiable.
    """

    def test_close_retires_the_stop_leg_first(self) -> None:
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp([_super_order()])],
            "DELETE /v2/super/orders/900/STOP_LOSS_LEG": [_Resp({"orderStatus": "CANCELLED"})],
            "POST /v2/orders": [_Resp({"orderId": "950", "orderStatus": "TRANSIT"})],
        })
        _client(http).place_order(
            OrderRequest(symbol="PIIND", side="sell", size=Decimal("15"),
                         order_type=OrderType.MARKET, reduce_only=True)
        )
        methods = [c["method"] for c in http.calls]
        assert methods == ["GET", "DELETE", "POST"], "cancel must precede the sell"

    def test_close_aborts_when_the_leg_cannot_be_cancelled(self) -> None:
        """Refusing to sell is recoverable — the position stays protected by
        the very leg we failed to cancel. Selling twice is not."""
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp([_super_order()])],
            "DELETE /v2/super/orders/900/STOP_LOSS_LEG": [
                _Resp({"errorCode": "DH-101", "errorMessage": "nope"})
            ],
        })
        with pytest.raises(AttachedStopRetireError):
            _client(http).place_order(
                OrderRequest(symbol="PIIND", side="sell", size=Decimal("15"),
                             order_type=OrderType.MARKET, reduce_only=True)
            )
        assert http.paths("POST") == [], "no closing order may be sent"

    def test_close_aborts_when_the_lookup_itself_fails(self) -> None:
        """An empty answer is indistinguishable from 'nothing attached'. Acting
        on that false negative is exactly how an orphan leg survives."""
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp({"errorCode": "500", "errorMessage": "down"})],
        })
        with pytest.raises(AttachedStopRetireError):
            _client(http).place_order(
                OrderRequest(symbol="PIIND", side="sell", size=Decimal("15"),
                             order_type=OrderType.MARKET, reduce_only=True)
            )
        assert http.paths("POST") == []

    def test_a_blocked_close_is_not_a_venue_rejection(self) -> None:
        """The distinction is load-bearing: REJECTED feeds check_reject_rate
        (3 per 15 min), so a few blocked exits would HALT the bucket for a
        reason that has nothing to do with rejected orders. Nothing was
        rejected here — nothing was even sent."""
        assert not issubclass(AttachedStopRetireError, DhanAPIError)

    def test_close_of_an_unattached_position_still_works(self) -> None:
        """Legacy positions (opened before this decision) have no leg to
        retire; the close must not be blocked by that."""
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp([])],
            "POST /v2/orders": [_Resp({"orderId": "950", "orderStatus": "TRANSIT"})],
        })
        _client(http).place_order(
            OrderRequest(symbol="PIIND", side="sell", size=Decimal("15"),
                         order_type=OrderType.MARKET, reduce_only=True)
        )
        assert http.paths("POST") == ["/v2/orders"]

    def test_another_symbols_leg_is_not_touched(self) -> None:
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp([_super_order(symbol="TBZ")])],
            "POST /v2/orders": [_Resp({"orderId": "950", "orderStatus": "TRANSIT"})],
        })
        _client(http).place_order(
            OrderRequest(symbol="PIIND", side="sell", size=Decimal("15"),
                         order_type=OrderType.MARKET, reduce_only=True)
        )
        assert http.paths("DELETE") == []

    def test_a_protective_stop_is_not_a_close(self) -> None:
        """A resting stop is reduce-only too. Treating it as a close would make
        the sweep cancel the venue's own protection to place its own."""
        http = _FakeHttp({
            "POST /v2/orders": [_Resp({"orderId": "960", "orderStatus": "TRANSIT"})],
        })
        _client(http).place_order(
            OrderRequest(symbol="PIIND", side="sell", size=Decimal("15"),
                         order_type=OrderType.MARKET, reduce_only=True,
                         stop_price=Decimal("2300")),
        )
        assert http.paths("GET") == [], "no super-order lookup for a resting stop"


# ── Ownership on the shared account (Decision 027) ───────────────────────
class TestOwnership:
    def test_our_correlation_id_proves_ownership(self) -> None:
        http = _FakeHttp({"GET /v2/super/orders": [_Resp([_super_order()])]})
        assert _client(http).attached_stop_triggers() == {"PIIND": Decimal("2314.40")}

    def test_na_correlation_id_is_not_ours(self) -> None:
        """Dhan returns the literal string "NA" for an order placed without a
        correlation id, and bool("NA") is True — the 2026-08-14 bug that had
        the bot queuing the user's own stop for cancellation."""
        http = _FakeHttp({"GET /v2/super/orders": [_Resp([_super_order(correlation="NA")])]})
        assert _client(http).attached_stop_triggers() == {}

    def test_ledger_proves_ownership_when_correlation_id_is_absent(self) -> None:
        """It is UNVERIFIED whether Dhan echoes correlationId onto super-order
        legs. If it does not, this fallback is the only thing standing between
        us and never recognising our own stops — which would mean never
        retiring one before a sell, and halting the bucket every tick."""
        http = _FakeHttp({"GET /v2/super/orders": [_Resp([_super_order(correlation="NA")])]})
        client = _client(http, owns=lambda oid: oid == "900")
        assert client.attached_stop_triggers() == {"PIIND": Decimal("2314.40")}

    def test_unknown_order_stays_the_users(self) -> None:
        http = _FakeHttp({"GET /v2/super/orders": [_Resp([_super_order(correlation="NA")])]})
        client = _client(http, owns=lambda oid: False)
        assert client.attached_stop_triggers() == {}

    def test_a_negative_ownership_answer_is_never_cached(self) -> None:
        """The ledger row appears moments after placement, so an early "no" is a
        statement about TIMING, not ownership. Caching it would permanently
        disown one of our own super orders — and the bot would then never retire
        its stop leg before selling."""
        calls: list[str] = []
        answers = iter([False, True])

        def owns(oid: str) -> bool:
            calls.append(oid)
            return next(answers)

        http = _FakeHttp({
            "GET /v2/super/orders": [
                _Resp([_super_order(correlation="NA")]),
                _Resp([_super_order(correlation="NA")]),
            ],
        })
        client = _client(http, owns=owns)
        assert client.attached_stop_triggers() == {}                      # early
        assert client.attached_stop_triggers() == {"PIIND": Decimal("2314.40")}
        assert calls == ["900", "900"], "the 'no' must be re-asked"

    def test_a_filled_stop_leg_is_no_longer_protection(self) -> None:
        http = _FakeHttp({
            "GET /v2/super/orders": [_Resp([_super_order(stop_status="TRADED")])]
        })
        assert _client(http).attached_stop_triggers() == {}


# ── Coexistence with the Decision 022 sweep ──────────────────────────────
class TestSweepCoexistence:
    def _pos(self) -> PositionInfo:
        return PositionInfo(symbol="PIIND", side="long", size=Decimal("15"),
                            entry_price=Decimal("2514.50"))

    def test_sweep_does_not_stack_a_second_stop(self) -> None:
        plan = plan_stop_protection(
            positions=[self._pos()],
            open_orders=[],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={"PIIND": ("swing-indian", "mean_reversion_1h")},
            attached_stops={"PIIND": Decimal("2314.40")},
        )
        assert plan.place == []
        assert plan.attached == ["PIIND"]

    def test_sweep_does_not_cancel_the_venues_own_leg(self) -> None:
        """The leg also appears in the plain order book. Without the skip the
        sweep reads it as a drifted/orphaned stop and cancels the protection
        the entry was placed with."""
        from src.brokers.base import OpenOrder

        leg = OpenOrder(
            exchange_order_id="900", client_order_id="cid-1", symbol="PIIND",
            side="sell", size=Decimal("15"), unfilled_size=Decimal("15"),
            order_type="STOP_LOSS_MARKET", limit_price=None, status="open",
            stop_price=Decimal("2314.40"), reduce_only=True,
        )
        plan = plan_stop_protection(
            positions=[self._pos()],
            open_orders=[leg],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={"PIIND": ("swing-indian", "mean_reversion_1h")},
            attached_stops={"PIIND": Decimal("2314.40")},
        )
        assert plan.cancel == []
        assert plan.place == []

    def test_orphaned_leg_is_queued_for_retirement(self) -> None:
        """The position ended without the bot closing it — Dhan's 15:20 MIS
        square-off, or a manual close. Nothing passed through the adapter's
        cancel-before-close guard, so only the sweep can catch this."""
        plan = plan_stop_protection(
            positions=[],
            open_orders=[],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={},
            attached_stops={"PIIND": Decimal("2314.40")},
        )
        assert plan.retire_legs == ["PIIND"]

    def test_a_users_holding_does_not_keep_our_leg_alive(self) -> None:
        """Shared account: the bot sold its 15, the user still holds 100 of the
        same scrip. A position row exists, but none of it is ours — our leg is
        orphaned and would sell stock we do not hold."""
        plan = plan_stop_protection(
            positions=[self._pos()],
            open_orders=[],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={},
            owned_quantities={},          # bot owns nothing
            attached_stops={"PIIND": Decimal("2314.40")},
        )
        assert plan.retire_legs == ["PIIND"]

    def test_a_brand_new_entry_is_never_called_orphaned(self) -> None:
        """THE RACE THIS PASS WOULD OTHERWISE LOSE. Between placing the super
        order and Dhan surfacing the position, "leg with no position" is also
        exactly what a two-second-old entry looks like. Retiring on that reading
        strips a live position of its only stop — strictly worse than the
        pre-034 race, which merely placed a duplicate."""
        plan = plan_stop_protection(
            positions=[],                       # Dhan has not reported it yet
            open_orders=[],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={},
            owned_quantities={"PIIND": Decimal("15")},
            attached_stops={"PIIND": Decimal("2314.40")},
            recent_entries={"PIIND"},
        )
        assert plan.retire_legs == []

    def test_the_grace_is_time_bounded_not_ledger_bounded(self) -> None:
        """A ledger check alone cannot do this job: owned_quantities counts an
        entry from PENDING and only decrements on a FILLED sell, so after an
        auto-square-off it reports the symbol held forever and the orphan would
        never be retired — defeating the pass entirely."""
        plan = plan_stop_protection(
            positions=[],
            open_orders=[],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={},
            owned_quantities={"PIIND": Decimal("15")},   # stale: no exit row
            attached_stops={"PIIND": Decimal("2314.40")},
            recent_entries=set(),                        # but not recent
        )
        assert plan.retire_legs == ["PIIND"]

    def test_held_position_keeps_its_leg(self) -> None:
        plan = plan_stop_protection(
            positions=[self._pos()],
            open_orders=[],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={},
            attached_stops={"PIIND": Decimal("2314.40")},
        )
        assert plan.retire_legs == []

    def test_a_leftover_standalone_stop_is_cancelled(self) -> None:
        """Two resting stops on one long is a double-sell: the second sells
        stock the first already sold. Exactly one per symbol, attached wins."""
        from src.brokers.base import OpenOrder

        legacy = OpenOrder(
            exchange_order_id="777", client_order_id="cid-old", symbol="PIIND",
            side="sell", size=Decimal("15"), unfilled_size=Decimal("15"),
            order_type="STOP_LOSS_MARKET", limit_price=None, status="open",
            stop_price=Decimal("2011.60"), reduce_only=True,   # the OLD 20% net
        )
        plan = plan_stop_protection(
            positions=[self._pos()],
            open_orders=[legacy],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={"PIIND": ("swing-indian", "mean_reversion_1h")},
            attached_stops={"PIIND": Decimal("2314.40")},
        )
        assert [o.exchange_order_id for o in plan.cancel] == ["777"]
        assert plan.place == []

    def test_unattached_position_is_swept_as_before(self) -> None:
        plan = plan_stop_protection(
            positions=[self._pos()],
            open_orders=[],
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attribution={"PIIND": ("swing-indian", "mean_reversion_1h")},
            attached_stops={},
        )
        assert [s.symbol for s in plan.place] == ["PIIND"]


# ── stop_coverage must not halt a super-order position ───────────────────
class TestStopCoverageInvariant:
    def test_attached_stop_counts_as_coverage(self) -> None:
        res = check_stop_coverage(
            bucket_id="swing-indian",
            holdings={"PIIND": Decimal("15")},
            open_orders=[],
            sustain_ticks=1,
            attached_stops={"PIIND": Decimal("2314.40")},
        )
        assert res.ok

    def test_uncovered_position_still_halts(self) -> None:
        res = check_stop_coverage(
            bucket_id="swing-indian",
            holdings={"PIIND": Decimal("15")},
            open_orders=[],
            sustain_ticks=1,
            attached_stops={},
        )
        assert not res.ok
        assert res.severity is Severity.HALT


# ── Shared orderId must not corrupt P&L ──────────────────────────────────
class TestFillAttribution:
    """Three legs share one orderId, so fills can no longer be bucketed by id
    alone: the stop's SELL would average into the entry's BUY and produce a
    price describing no trade that ever happened.
    """

    class _Fill:
        def __init__(self, side: str, price: str) -> None:
            self.side = side
            self.price = Decimal(price)

    class _Trade:
        def __init__(self, side: str) -> None:
            self.side = type("S", (), {"value": side})()

    def test_stop_leg_fill_does_not_blend_into_the_entry(self) -> None:
        from src.order_manager.reconciler import _fills_for_trade

        fills = [self._Fill("buy", "2514.50"), self._Fill("sell", "2314.40")]
        kept = _fills_for_trade(fills, self._Trade("buy"))
        assert [f.price for f in kept] == [Decimal("2514.50")]

    def test_sideless_fills_are_kept(self) -> None:
        """For every non-super order the id match is already conclusive;
        dropping these would silently disable P&L on a sparse trade book."""
        from src.order_manager.reconciler import _fills_for_trade

        kept = _fills_for_trade([self._Fill("", "100")], self._Trade("buy"))
        assert len(kept) == 1


# ── Trigger / target arithmetic ──────────────────────────────────────────
class TestPriceResolution:
    ENTRY = Decimal("2514.50")

    def test_strategy_distance_tightens_the_bucket_net(self) -> None:
        """PIIND's real numbers: the ATR stop is what the backtest validated."""
        trigger = resolve_stop_trigger(
            entry_price=self.ENTRY, position_side="long",
            stop_pct=Decimal("20"), distance=Decimal("200.089"),
            tick=Decimal("0.05"), symbol="PIIND",
        )
        assert trigger == Decimal("2314.40")

    def test_a_wider_strategy_distance_is_ignored(self) -> None:
        trigger = resolve_stop_trigger(
            entry_price=self.ENTRY, position_side="long",
            stop_pct=Decimal("10"), distance=Decimal("900"),
            tick=Decimal("0.05"),
        )
        assert trigger == Decimal("2263.05")  # the 10% net, not the 900 distance

    def test_band_clamp_only_ever_tightens(self) -> None:
        """A -20% trigger on a 10%-band scrip is UNPLACEABLE — the exchange
        refuses it and the position ends up with no stop at all."""
        trigger = resolve_stop_trigger(
            entry_price=self.ENTRY, position_side="long",
            stop_pct=Decimal("20"), band_pct=Decimal("10"),
            tick=Decimal("0.05"), symbol="PIIND",
        )
        assert trigger == Decimal("2288.20")
        assert trigger > self.ENTRY * Decimal("0.8")

    def test_target_sits_inside_the_band(self) -> None:
        """Not "far away": a target outside the band rejects the WHOLE super
        order, entry included."""
        target = resolve_target_price(
            entry_price=self.ENTRY, position_side="long",
            band_pct=Decimal("10"), tick=Decimal("0.05"),
        )
        assert target == Decimal("2740.80")
        assert target < self.ENTRY * Decimal("1.10")

    def test_unknown_band_falls_back_tight(self) -> None:
        """Erring tight risks an unwanted profit-take only if the cancel ALSO
        failed; erring wide kills the entry outright."""
        target = resolve_target_price(
            entry_price=Decimal("100"), position_side="long", tick=Decimal("0.05")
        )
        assert target == Decimal("104.00")

    def test_short_side_is_mirrored(self) -> None:
        assert resolve_target_price(
            entry_price=Decimal("100"), position_side="short", tick=Decimal("0.05")
        ) == Decimal("96.00")
        assert resolve_stop_trigger(
            entry_price=Decimal("100"), position_side="short",
            stop_pct=Decimal("10"), tick=Decimal("0.05"),
        ) == Decimal("110.00")


# ── Recording exits the bot never sent ───────────────────────────────────
class TestUnrecordedExitDetection:
    """The bot counts what it owns by adding its BUY rows and subtracting its
    SELL rows. That balances only while every sale passes through OrderManager
    — which a Decision 034 stop leg, Dhan's 15:20 auto-square-off, and the
    user's own manual sells all bypass. The ledger is what keeps the bot off
    the user's stock (Decision 027), so an over-count is a real hazard.

    These pin the CONFIRMATION policy, which is the dangerous half: acting on
    one bad read would fabricate an exit and make the bot abandon a position it
    still holds.
    """

    def _rec(self):
        from src.core.models import BrokerName
        from src.order_manager.reconciler import Reconciler

        return Reconciler(
            broker=None,  # type: ignore[arg-type]
            broker_name=BrokerName.DHAN,
            bucket_ids=["swing-indian"],
            shared_account=True,
        )

    def test_a_shortfall_must_survive_three_passes(self) -> None:
        rec = self._rec()
        gap = {"PIIND": Decimal("15")}
        assert rec._confirm_shortfalls(gap) == {}
        assert rec._confirm_shortfalls(gap) == {}
        assert rec._confirm_shortfalls(gap) == {"PIIND": Decimal("15")}

    def test_a_transient_bad_read_never_fabricates_an_exit(self) -> None:
        """get_positions fails SOFT on the holdings leg — one errored
        /v2/holdings makes every settled swing holding look sold."""
        rec = self._rec()
        assert rec._confirm_shortfalls({"PIIND": Decimal("15")}) == {}
        assert rec._confirm_shortfalls({}) == {}          # holdings came back
        assert rec._confirm_shortfalls({"PIIND": Decimal("15")}) == {}
        assert rec._shortfall_seen["PIIND"][1] == 1, "the count must restart"

    def test_a_moving_shortfall_restarts_the_count(self) -> None:
        """A position being sold down in pieces is still moving; the moment to
        record it is once it has settled."""
        rec = self._rec()
        rec._confirm_shortfalls({"PIIND": Decimal("5")})
        rec._confirm_shortfalls({"PIIND": Decimal("5")})
        assert rec._confirm_shortfalls({"PIIND": Decimal("10")}) == {}
        assert rec._confirm_shortfalls({"PIIND": Decimal("10")}) == {}
        assert rec._confirm_shortfalls({"PIIND": Decimal("10")}) == {
            "PIIND": Decimal("10")
        }

    def test_crypto_accounts_are_left_alone(self) -> None:
        """Exclusive sub-accounts (Decision 019) trust exchange positions
        directly, so the Trade ledger drives nothing there."""
        from src.core.models import BrokerName
        from src.order_manager.reconciler import Reconciler, ReconcileReport

        rec = Reconciler(
            broker=None,  # type: ignore[arg-type]
            broker_name=BrokerName.DELTA_INDIA,
            bucket_ids=["longterm-crypto"],
            shared_account=False,
        )
        report = ReconcileReport()
        rec._detect_unrecorded_exits(report, positions=[])   # must not touch the broker
        assert report.diffs == []


# ── The dark deploy must be genuinely inert ──────────────────────────────
class TestFeatureGating:
    """DhanClient.supports_attached_stop() is True the moment this code
    deploys. Keying the super-order reads off CAPABILITY rather than the
    feature switch would fire GET /v2/super/orders every tick on an account
    where nothing is enabled — and because the sweep abandons itself when that
    lookup fails, an endpoint the account cannot use would have silently
    disabled protective stops on both live Indian buckets.
    """

    class _Spy:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def supports_attached_stop(self) -> bool:
            return True

        def attached_stop_triggers(self) -> dict:
            self.calls.append("super")
            return {}

        def get_positions(self) -> list:
            return []

        def get_open_orders(self) -> list:
            return []

        def tick_size(self, symbol: str):
            return Decimal("0.05")

    def _sweep(self, enabled: bool) -> list[str]:
        from src.safety.stop_protection import ensure_stop_protection

        broker = self._Spy()
        ensure_stop_protection(
            account_ref="dhan",
            bucket_ids=["swing-indian"],
            broker=broker,  # type: ignore[arg-type]
            order_manager=None,  # type: ignore[arg-type]
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            attached_stops_enabled=enabled,
        )
        return broker.calls

    def test_switch_off_never_touches_the_super_order_endpoint(self) -> None:
        assert self._sweep(False) == []

    def test_switch_on_reads_the_legs(self) -> None:
        assert self._sweep(True) == ["super"]
