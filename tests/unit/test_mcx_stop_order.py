"""MCX strips a stop's trigger, so the adapter must not send one blind.

On 2026-09-02 the stop sweep placed what it believed was a protective
stop-market on a live NATGASMINI lot:

    sent      orderType=STOP_LOSS_MARKET  triggerPrice=269.60  price=0
    booked    SELL TRADED  type=LIMIT  triggerPrice=0.0  price=266.9  avg=281.5

The trigger was gone. What rested was a plain limit sell below market, so it
filled INSTANTLY at 281.50 and closed the position it existed to protect. The
same conversion is visible on that session's entry (MARKET booked as LIMIT
285.00), so this is MCX market-protection applied to a stop and losing the
trigger on the way through.

That makes a standalone MCX stop worse than none — not "might not fire" but
"fires immediately, every time" — and there is no safe limit price to retreat
to, because any sell at or below market is marketable the moment the trigger is
dropped. So the adapter REFUSES until the behaviour is proven, and a stop-LIMIT
(which carries its own price, leaving nothing for market-protection to invent)
is the form MCX is given once it is.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.brokers.base import OrderRequest, OrderType
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanAPIError, DhanClient

_UNIVERSE = {
    "NATGASMINI-20260925-FUT": ("568246", "MCX_COMM"),
    "SWIGGY": ("1001", "NSE_EQ"),
}


def _resolve(symbol: str) -> tuple[str, str]:
    return _UNIVERSE[symbol]


class _Resp:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method, url, json=None, headers=None):  # noqa: A002
        self.calls.append({"method": method, "url": url, "json": json})
        # TRADED so the adapter's async-reject verification settles at once
        # instead of backing off through its retry ladder.
        return _Resp({"orderId": "1", "orderStatus": "TRADED"})


def _client(http: _FakeHttp) -> DhanClient:
    return DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=_resolve,
        base_url="https://api.dhan.co",
        http=http,
    )


def _post_body(http: _FakeHttp) -> dict:
    """The order POST. The adapter also GETs the book to catch Dhan's
    ASYNCHRONOUS rejects, so the placement is not simply calls[0]."""
    posts = [c for c in http.calls if c["method"] == "POST" and c["json"]]
    assert posts, f"no order POST in {http.calls}"
    return posts[0]["json"]


def _verified(monkeypatch, value: bool) -> None:
    """Flip Settings.mcx_standalone_stops_verified for the adapter."""
    import src.brokers.dhan.client as mod

    real = mod.get_settings()

    class _S:
        def __getattr__(self, name):  # noqa: ANN001
            if name == "mcx_standalone_stops_verified":
                return value
            return getattr(real, name)

    monkeypatch.setattr(mod, "get_settings", lambda: _S())


def _stop_req(symbol: str, trigger: str, side: str = "sell") -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        size=Decimal("1"),
        order_type=OrderType.MARKET,
        stop_price=Decimal(trigger),
        reduce_only=True,
        client_order_id="stop-1",
    )


# ── the refusal ─────────────────────────────────────────────────────────


def test_standalone_mcx_stop_is_refused_until_proven(monkeypatch) -> None:
    """The live failure. Refusing surfaces as stop_place_failed and pages
    about an uncovered position, instead of silently liquidating it."""
    _verified(monkeypatch, False)
    http = _FakeHttp()

    with pytest.raises(DhanAPIError) as exc:
        _client(http).place_order(_stop_req("NATGASMINI-20260925-FUT", "269.60"))

    assert exc.value.code == "MCX_STOP_UNVERIFIED"
    assert http.calls == [], "nothing may reach the venue"


def test_nse_stops_are_untouched_by_the_guard(monkeypatch) -> None:
    """NSE has always honoured STOP_LOSS_MARKET; the guard must not reach it."""
    _verified(monkeypatch, False)
    http = _FakeHttp()

    _client(http).place_order(_stop_req("SWIGGY", "2400"))

    body = _post_body(http)
    assert body["orderType"] == "STOP_LOSS_MARKET"
    assert body["triggerPrice"] == 2400.0
    assert body["price"] == 0


def test_a_plain_mcx_order_is_not_blocked(monkeypatch) -> None:
    """Only STOPS are refused — entries and exits must still go through."""
    _verified(monkeypatch, False)
    http = _FakeHttp()

    _client(http).place_order(
        OrderRequest(
            symbol="NATGASMINI-20260925-FUT",
            side="sell",
            size=Decimal("1"),
            order_type=OrderType.MARKET,
            reduce_only=True,
        )
    )
    assert _post_body(http)["orderType"] == "MARKET"


# ── the payload, once proven ────────────────────────────────────────────


def test_proven_mcx_stop_is_a_stop_limit_priced_through_the_trigger(
    monkeypatch,
) -> None:
    """A stop-LIMIT carries its own price, so market-protection has nothing
    to invent and no trigger to drop."""
    _verified(monkeypatch, True)
    http = _FakeHttp()

    _client(http).place_order(_stop_req("NATGASMINI-20260925-FUT", "269.60"))

    body = _post_body(http)
    assert body["orderType"] == "STOP_LOSS"
    assert body["triggerPrice"] == 269.6
    # 1% THROUGH the trigger for a sell — fills on the way down, snapped to
    # the contract's own grid.
    assert body["price"] == pytest.approx(266.9, abs=0.11)
    assert body["price"] < body["triggerPrice"], "a sell must rest below"


def test_a_buy_stop_rests_above_its_trigger(monkeypatch) -> None:
    """Short protection is the mirror image; getting the sign wrong would
    make the stop unfillable rather than merely wide."""
    _verified(monkeypatch, True)
    http = _FakeHttp()

    _client(http).place_order(
        _stop_req("NATGASMINI-20260925-FUT", "290.00", side="buy")
    )

    body = _post_body(http)
    assert body["orderType"] == "STOP_LOSS"
    assert body["price"] > body["triggerPrice"], "a buy must rest above"
