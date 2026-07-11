"""Dhan broker client — order build, MTF fallback, stops, parsing (Phase 3/4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.brokers.base import OrderRequest, OrderType
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanAPIError, DhanClient

_UNIVERSE = {
    "SWIGGY": ("1001", "NSE_EQ"),
    "TBZ": ("2002", "BSE_EQ"),
}


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
    """Routes by ``"<METHOD> <suffix>"`` to queued responses; records calls."""

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


def _client(http: _FakeHttp, *, product: str = "MTF", fallback: bool = True) -> DhanClient:
    token = DhanTokenManager(static_token="TOK")
    return DhanClient(
        token_manager=token, client_id="C1", resolve_symbol=_resolve,
        base_url="https://sandbox.dhan.co", product_type=product,
        mtf_fallback_cnc=fallback, http=http,
    )


def test_place_market_order_builds_body() -> None:
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"orderId": "555", "orderStatus": "TRANSIT"})]})
    req = OrderRequest(symbol="SWIGGY", side="buy", size=Decimal("30"),
                       order_type=OrderType.MARKET, client_order_id="abc123")
    res = _client(http).place_order(req)
    body = http.calls[0]["json"]
    assert body["securityId"] == "1001"
    assert body["exchangeSegment"] == "NSE_EQ"
    assert body["transactionType"] == "BUY"
    assert body["productType"] == "MTF"
    assert body["orderType"] == "MARKET"
    assert body["quantity"] == 30
    assert body["correlationId"] == "abc123"
    assert res.exchange_order_id == "555"
    assert res.status == "pending"


def test_mtf_rejection_falls_back_to_cnc() -> None:
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"errorType": "Order_Error", "errorCode": "DH-XXX",
               "errorMessage": "MTF not allowed"}),
        _Resp({"orderId": "777", "orderStatus": "PENDING"})]})
    req = OrderRequest(symbol="TBZ", side="buy", size=Decimal("10"))
    res = _client(http).place_order(req)
    assert len(http.calls) == 2
    assert http.calls[0]["json"]["productType"] == "MTF"
    assert http.calls[1]["json"]["productType"] == "CNC"
    assert res.raw["productType"] == "CNC"
    assert res.exchange_order_id == "777"


def test_mtf_rejection_no_fallback_raises() -> None:
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"errorCode": "DH-XXX", "errorMessage": "nope"})]})
    req = OrderRequest(symbol="TBZ", side="buy", size=Decimal("10"))
    with pytest.raises(DhanAPIError):
        _client(http, fallback=False).place_order(req)


def test_stop_order_uses_trigger_and_snaps_tick() -> None:
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"orderId": "9", "orderStatus": "PENDING"})]})
    # 233.33 snaps to the ₹0.05 grid → 233.35
    req = OrderRequest(symbol="SWIGGY", side="sell", size=Decimal("30"),
                       order_type=OrderType.MARKET, reduce_only=True,
                       stop_price=Decimal("233.33"))
    _client(http).place_order(req)
    body = http.calls[0]["json"]
    assert body["orderType"] == "STOP_LOSS_MARKET"
    assert body["transactionType"] == "SELL"
    assert body["triggerPrice"] == 233.35


def test_get_positions_maps_sign_and_skips_flat() -> None:
    http = _FakeHttp({"GET /v2/positions": [_Resp([
        {"tradingSymbol": "SWIGGY", "securityId": "1001", "netQty": 30,
         "buyAvg": 280.0, "unrealizedProfit": 150.0},
        {"tradingSymbol": "TBZ", "securityId": "2002", "netQty": 0},  # squared off
    ])]})
    pos = _client(http).get_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "SWIGGY"
    assert pos[0].side == "long"
    assert pos[0].size == Decimal("30")
    assert pos[0].entry_price == Decimal("280.0")
    assert pos[0].unrealized_pnl == Decimal("150.0")


def test_get_balances_reads_fundlimit() -> None:
    http = _FakeHttp({"GET /v2/fundlimit": [_Resp(
        {"availabelBalance": 45000.5, "utilizedAmount": 5000.0})]})
    bal = _client(http).get_balances()
    assert bal[0].asset == "INR"
    assert bal[0].available == Decimal("45000.5")
    assert bal[0].position_margin == Decimal("5000.0")


def test_get_open_orders_filters_and_maps() -> None:
    http = _FakeHttp({"GET /v2/orders": [_Resp([
        {"orderId": "1", "orderStatus": "PENDING", "tradingSymbol": "SWIGGY",
         "transactionType": "BUY", "quantity": 30, "orderType": "MARKET"},
        {"orderId": "2", "orderStatus": "TRADED", "tradingSymbol": "TBZ",
         "transactionType": "BUY", "quantity": 5},  # filled → excluded
    ])]})
    orders = _client(http).get_open_orders()
    assert [o.exchange_order_id for o in orders] == ["1"]
    assert orders[0].status == "pending"


def test_get_order_by_client_id_scans_correlation() -> None:
    http = _FakeHttp({"GET /v2/orders": [_Resp([
        {"orderId": "1", "orderStatus": "PENDING", "correlationId": "xyz",
         "tradingSymbol": "SWIGGY", "transactionType": "BUY", "quantity": 30},
    ])]})
    o = _client(http).get_order_by_client_id("xyz")
    assert o is not None and o.exchange_order_id == "1"


def test_get_fills_parses_trades() -> None:
    http = _FakeHttp({"GET /v2/trades": [_Resp([
        {"orderId": "1", "exchangeTradeId": "T1", "tradingSymbol": "SWIGGY",
         "transactionType": "BUY", "tradedQuantity": 30, "tradedPrice": 281.2},
    ])]})
    fills = _client(http).get_fills()
    assert fills[0].size == Decimal("30")
    assert fills[0].price == Decimal("281.2")
    assert fills[0].side == "buy"


def test_error_envelope_raises() -> None:
    http = _FakeHttp({"GET /v2/fundlimit": [_Resp(
        {"errorType": "Auth", "errorCode": "DH-906", "errorMessage": "Invalid Token"})]})
    with pytest.raises(DhanAPIError, match="DH-906"):
        _client(http).get_balances()


def test_non_json_403_raises_clean_error() -> None:
    # A 403 IP-block page has an empty/non-JSON body — must surface as a clean
    # DhanAPIError with the status code, not a leaked JSONDecodeError.
    class _RawResp:
        status_code = 403
        text = ""

        def json(self):
            raise ValueError("no json")

    class _RawHttp:
        def request(self, *a, **k):
            return _RawResp()

    client = DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"), client_id="C1",
        resolve_symbol=_resolve, http=_RawHttp(),
    )
    with pytest.raises(DhanAPIError, match="403"):
        client.get_balances()


def test_set_leverage_is_noop() -> None:
    http = _FakeHttp({})  # no routes → any HTTP call would AssertionError
    _client(http).set_leverage("SWIGGY", Decimal("3"))  # must not hit the wire
    assert http.calls == []


def test_contract_and_tick_size() -> None:
    c = _client(_FakeHttp({}))
    assert c.contract_size("SWIGGY") == Decimal("1")
    assert c.tick_size("SWIGGY") == Decimal("0.05")


def test_401_invalidates_and_retries() -> None:
    http = _FakeHttp({"GET /v2/fundlimit": [
        _Resp({}, status=401),
        _Resp({"availabelBalance": 100}),
    ]})

    class _TokHttp:
        def __init__(self) -> None:
            self.n = 0

        def post(self, url, params=None):
            self.n += 1
            import base64 as b64
            import json as j
            raw = j.dumps({"exp": 9_999_999_999}).encode()
            exp = b64.urlsafe_b64encode(raw).decode().rstrip("=")
            return _Resp({"accessToken": f"h.{exp}.s"})

    token = DhanTokenManager(client_id="c", pin="p",
                             totp_secret="JBSWY3DPEHPK3PXP", http=_TokHttp())
    client = DhanClient(token_manager=token, client_id="C1",
                        resolve_symbol=_resolve, http=http)
    bal = client.get_balances()
    assert bal[0].available == Decimal("100")
    assert len(http.calls) == 2  # 401 then retry
