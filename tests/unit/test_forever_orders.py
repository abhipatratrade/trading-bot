"""Forever Orders (GTT) — visibility to the stop sweep, and cancel routing.

Decision 035/037. Dhan accepts a Forever Order on ``MCX_COMM`` with
``productType: MARGIN`` — proven live 2026-08-31, orderId 23132608311510 —
which makes a venue-resident stop that survives a session close possible, and
makes a NEW failure mode possible along with it.

A working stop carries ``validity: DAY``, so an orphan expires by itself: it
FAILS SAFE. A forever order rests for up to 365 days with no link to any
position, so an orphan OPENS a position when it triggers: it FAILS OPEN.

These tests pin the two properties that stop that happening — the sweep can SEE
a resting GTT, and a cancel reaches the order book the GTT actually lives in —
plus the Decision 027 property that must survive both: a GTT the USER placed by
hand in the Dhan app is invisible to a planner whose job is cancelling things.
"""

from __future__ import annotations

from decimal import Decimal

from src.brokers.base import OpenOrder, PositionInfo
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanClient
from src.safety.stop_protection import plan_stop_protection

_UNIVERSE = {"NATGASMINI-20260925-FUT": ("568246", "MCX_COMM")}


def _resolve(symbol: str) -> tuple[str, str]:
    return _UNIVERSE[symbol]


class _Resp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


class _FakeHttp:
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


def _client(http: _FakeHttp) -> DhanClient:
    return DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=_resolve,
        base_url="https://api.dhan.co",
        http=http,
    )


def _gtt(
    *,
    order_id: str = "GTT1",
    correlation: str | None = "stop-135-1-202608311510",
    status: str = "PENDING",
    trigger: object = 135.0,
    price: object = 135.0,
    qty: int = 1,
    side: str = "SELL",
) -> dict:
    body = {
        "orderId": order_id,
        "orderStatus": status,
        "transactionType": side,
        "exchangeSegment": "MCX_COMM",
        "productType": "MARGIN",
        "orderType": "LIMIT",
        "tradingSymbol": "NATGASMINI-20260925-FUT",
        "securityId": "568246",
        "quantity": qty,
    }
    if trigger is not None:
        body["triggerPrice"] = trigger
    if price is not None:
        body["price"] = price
    if correlation is not None:
        body["correlationId"] = correlation
    return body


# ── parsing ─────────────────────────────────────────────────────────────


def test_our_gtt_is_a_visible_reduce_only_stop() -> None:
    http = _FakeHttp({"GET /v2/forever/orders": [_Resp([_gtt()])]})
    orders = _client(http).get_forever_orders()

    assert len(orders) == 1
    o = orders[0]
    assert o.forever is True
    assert o.stop_price == Decimal("135.0")
    # reduce_only is what makes plan_stop_protection consider it at all.
    assert o.reduce_only is True


def test_user_placed_gtt_is_invisible_to_the_planner() -> None:
    """A GTT from the Dhan app has no correlationId of ours (Decision 027).

    ``reduce_only`` False is not a cosmetic label here — it is the whole
    guard. plan_stop_protection only ever collects, and therefore only ever
    CANCELS, orders that carry it.
    """
    http = _FakeHttp(
        {"GET /v2/forever/orders": [_Resp([_gtt(correlation=None)])]}
    )
    o = _client(http).get_forever_orders()[0]

    assert o.forever is True
    assert o.reduce_only is False
    assert o.client_order_id is None


def test_dhan_na_correlation_is_not_ours() -> None:
    """Dhan returns the literal string "NA", and bool("NA") is True."""
    http = _FakeHttp(
        {"GET /v2/forever/orders": [_Resp([_gtt(correlation="NA")])]}
    )
    assert _client(http).get_forever_orders()[0].reduce_only is False


def test_unknown_status_still_counts_as_resting() -> None:
    """Fail toward VISIBILITY: an unlisted state must not hide a live GTT."""
    http = _FakeHttp(
        {"GET /v2/forever/orders": [_Resp([_gtt(status="SOME_NEW_STATE")])]}
    )
    assert len(_client(http).get_forever_orders()) == 1


def test_finished_gtts_are_dropped() -> None:
    http = _FakeHttp({"GET /v2/forever/orders": [_Resp([
        _gtt(order_id="A", status="CANCELLED"),
        _gtt(order_id="B", status="TRIGGERED"),
        _gtt(order_id="C", status="PENDING"),
    ])]})
    assert [o.exchange_order_id for o in _client(http).get_forever_orders()] == ["C"]


def test_trigger_falls_back_to_price() -> None:
    """The 2026-08-25 super-order bug in miniature: the trigger came back
    under ``price``. A stop parsed with no trigger is a stop the planner
    cannot match, so it would place a second one on top."""
    http = _FakeHttp(
        {"GET /v2/forever/orders": [_Resp([_gtt(trigger=None, price=135.0)])]}
    )
    assert _client(http).get_forever_orders()[0].stop_price == Decimal("135.0")


# ── cancel routing ──────────────────────────────────────────────────────


def test_cancel_routes_to_the_forever_book() -> None:
    """The two books are separate; the wrong DELETE can report success while
    the order keeps resting for a year."""
    http = _FakeHttp({
        "GET /v2/forever/orders": [_Resp([_gtt(order_id="GTT9")])],
        "DELETE /v2/forever/orders/GTT9": [
            _Resp({"orderId": "GTT9", "orderStatus": "CANCELLED"})
        ],
    })
    client = _client(http)
    client.get_forever_orders()  # populates the routing set

    res = client.cancel_order(exchange_order_id="GTT9", symbol="NATGASMINI-20260925-FUT")

    assert res.success is True
    assert http.calls[-1]["url"].endswith("/v2/forever/orders/GTT9")


def test_cancel_routes_to_working_orders_when_not_a_gtt() -> None:
    http = _FakeHttp({
        "DELETE /v2/orders/W1": [_Resp({"orderId": "W1", "orderStatus": "CANCELLED"})],
    })
    client = _client(http)

    client.cancel_order(exchange_order_id="W1", symbol="NATGASMINI-20260925-FUT")

    assert http.calls[-1]["url"].endswith("/v2/orders/W1")


# ── the planner treats a GTT as the stop it is ──────────────────────────

_PCTS = {"commodity-indian": Decimal("4.5")}
_ATTR = {"NATGASMINI-20260925-FUT": ("commodity-indian", "cci_gas_reversion_15m")}


def _forever_stop(
    *, trigger: str = "258.0", size: str = "1", reduce_only: bool = True
) -> OpenOrder:
    return OpenOrder(
        exchange_order_id="GTT1",
        client_order_id="stop-1",
        symbol="NATGASMINI-20260925-FUT",
        side="sell",
        size=Decimal(size),
        unfilled_size=Decimal(size),
        order_type="LIMIT",
        limit_price=None,
        status="open",
        stop_price=Decimal(trigger),
        reduce_only=reduce_only,
        forever=True,
    )


def test_orphaned_forever_stop_is_cancelled() -> None:
    """No position behind it. This is the FAILS-OPEN case: left resting, a
    SELL stop on a closed long opens a short whenever price touches it."""
    plan = plan_stop_protection(
        positions=[],
        open_orders=[_forever_stop()],
        stop_pct_by_bucket=_PCTS,
        attribution=_ATTR,
        owned_quantities={"NATGASMINI-20260925-FUT": Decimal("1")},
    )
    assert [o.exchange_order_id for o in plan.cancel] == ["GTT1"]


def test_users_orphaned_gtt_is_left_alone() -> None:
    """Same shape, but not ours — so it must survive the sweep untouched."""
    plan = plan_stop_protection(
        positions=[],
        open_orders=[_forever_stop(reduce_only=False)],
        stop_pct_by_bucket=_PCTS,
        attribution=_ATTR,
        owned_quantities={"NATGASMINI-20260925-FUT": Decimal("1")},
    )
    assert plan.cancel == []


def test_matching_forever_stop_is_kept_not_replaced() -> None:
    """A GTT at the right level satisfies the position, exactly as a working
    stop would — otherwise the sweep rests a second one every tick."""
    pos = PositionInfo(
        symbol="NATGASMINI-20260925-FUT",
        side="long",
        size=Decimal("1"),
        entry_price=Decimal("270"),
    )
    # 4.5% below 270 = 257.85, snapped by the planner's own tick handling.
    plan = plan_stop_protection(
        positions=[pos],
        open_orders=[_forever_stop(trigger="257.85")],
        stop_pct_by_bucket=_PCTS,
        attribution=_ATTR,
        owned_quantities={"NATGASMINI-20260925-FUT": Decimal("1")},
    )
    assert plan.place == []
    assert plan.cancel == []
