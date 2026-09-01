"""Dhan broker client — order build, MTF fallback, stops, parsing (Phase 3/4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.brokers.base import ContractSpec, OrderRequest, OrderType
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import (
    DhanAPIError,
    DhanClient,
    is_invalid_token_error,
    is_transient_upstream_error,
)

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


def test_mtf_rejection_falls_back_to_cnc_at_1x_size() -> None:
    # Decision 032: the MTF retry is size-capped exactly like the MIS one. The
    # order was sized at the scrip's MTF multiple, so re-sending that quantity
    # as cash would spend `leverage`x the margin the sizer budgeted — on an
    # account shared with the user's own money.
    http = _FakeHttp({
        "POST /v2/orders": [
            _Resp({"errorType": "Order_Error", "errorCode": "DH-XXX",
                   "errorMessage": "MTF not allowed"}),
            _Resp({"orderId": "777", "orderStatus": "PENDING"})],
        # An accepted order is now confirmed against the order book before
        # place_order returns — Dhan's RMS rejects asynchronously.
        "GET /v2/orders/777": [_Resp({"orderStatus": "OPEN"})]})
    req = OrderRequest(symbol="TBZ", side="buy", size=Decimal("38"),
                       fallback_max_size=Decimal("10"))
    res = _client(http).place_order(req)
    posts = [c for c in http.calls if c["method"] == "POST"]
    assert len(posts) == 2
    assert posts[0]["json"]["productType"] == "MTF"
    assert posts[0]["json"]["quantity"] == 38
    assert posts[1]["json"]["productType"] == "CNC"
    assert posts[1]["json"]["quantity"] == 10
    assert res.raw["productType"] == "CNC"
    assert res.size == Decimal("10")
    assert res.exchange_order_id == "777"


def test_mtf_rejection_without_size_cap_raises() -> None:
    """No ``fallback_max_size`` ⇒ the rejection stands, never an uncapped retry."""
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"errorCode": "DH-XXX", "errorMessage": "MTF not allowed"}),
        _Resp({"orderId": "777", "orderStatus": "PENDING"})]})
    req = OrderRequest(symbol="TBZ", side="buy", size=Decimal("38"))
    with pytest.raises(DhanAPIError):
        _client(http).place_order(req)
    assert len(http.calls) == 1


def test_mtf_rejection_no_fallback_raises() -> None:
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"errorCode": "DH-XXX", "errorMessage": "nope"})]})
    req = OrderRequest(symbol="TBZ", side="buy", size=Decimal("10"))
    with pytest.raises(DhanAPIError):
        _client(http, fallback=False).place_order(req)


def test_mis_rejection_falls_back_to_cnc_at_1x_size() -> None:
    # Decision 029 (amended): an MIS-ineligible scrip retries as CNC, and the
    # size MUST drop to the 1x-affordable quantity — re-sending the leveraged
    # size on a cash product would spend `leverage`x the budgeted margin.
    http = _FakeHttp({
        "POST /v2/orders": [
            _Resp({"errorCode": "DH-XXX", "errorMessage": "MIS not allowed"}),
            _Resp({"orderId": "888", "orderStatus": "PENDING"})],
        "GET /v2/orders/888": [_Resp({"orderStatus": "OPEN"})]})
    req = OrderRequest(symbol="SWIGGY", side="buy", size=Decimal("40"),
                       product="INTRADAY", fallback_max_size=Decimal("10"))
    res = _client(http).place_order(req)
    posts = [c for c in http.calls if c["method"] == "POST"]
    assert len(posts) == 2
    assert posts[0]["json"]["productType"] == "INTRADAY"
    assert posts[0]["json"]["quantity"] == 40
    assert posts[1]["json"]["productType"] == "CNC"
    assert posts[1]["json"]["quantity"] == 10, "must clamp to 1x size"
    # The result reports what was actually placed, so the Trade row is honest.
    assert res.size == Decimal("10")
    assert res.raw["productType"] == "CNC"


def test_mis_rejection_without_cap_raises() -> None:
    # No fallback_max_size ⇒ bucket didn't opt in ⇒ the rejection stands.
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"errorCode": "DH-XXX", "errorMessage": "MIS not allowed"})]})
    req = OrderRequest(symbol="SWIGGY", side="buy", size=Decimal("40"),
                       product="INTRADAY")
    with pytest.raises(DhanAPIError):
        _client(http).place_order(req)
    assert len(http.calls) == 1, "must not retry without an explicit 1x cap"


def test_mis_fallback_never_upsizes() -> None:
    # A cap ABOVE the requested size must not inflate the order.
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"errorCode": "DH-XXX", "errorMessage": "MIS not allowed"}),
        _Resp({"orderId": "9", "orderStatus": "PENDING"})]})
    req = OrderRequest(symbol="SWIGGY", side="buy", size=Decimal("5"),
                       product="INTRADAY", fallback_max_size=Decimal("50"))
    res = _client(http).place_order(req)
    assert http.calls[1]["json"]["quantity"] == 5
    assert res.size == Decimal("5")


def test_mis_fallback_below_one_share_raises() -> None:
    # Budget can't afford a single share at 1x → surface the rejection rather
    # than place a 0-quantity order.
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"errorCode": "DH-XXX", "errorMessage": "MIS not allowed"})]})
    req = OrderRequest(symbol="SWIGGY", side="buy", size=Decimal("40"),
                       product="INTRADAY", fallback_max_size=Decimal("0"))
    with pytest.raises(DhanAPIError):
        _client(http).place_order(req)
    assert len(http.calls) == 1


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
    # A NON-token error raises immediately, no retry.
    http = _FakeHttp({"GET /v2/fundlimit": [_Resp(
        {"errorType": "Data", "errorCode": "DH-905", "errorMessage": "Bad request"})]})
    with pytest.raises(DhanAPIError, match="DH-905"):
        _client(http).get_balances()
    assert len(http.calls) == 1, "non-token error must not retry"


def test_invalid_token_retries_then_recovers() -> None:
    # DH-906 on the first call → invalidate + retry → success on the second.
    http = _FakeHttp({"GET /v2/fundlimit": [
        _Resp({"errorType": "Auth", "errorCode": "DH-906",
               "errorMessage": "Invalid Token"}, status=400),
        _Resp({"availabelBalance": 50000.0, "utilizedAmount": 0.0}),
    ]})
    _client(http).get_balances()
    assert len(http.calls) == 2, "must retry once after an invalid-token error"


def test_invalid_token_persists_raises() -> None:
    # Both attempts return DH-906 → the error surfaces (no infinite loop).
    env = {"errorType": "Auth", "errorCode": "DH-906",
           "errorMessage": "Invalid Token"}
    http = _FakeHttp({"GET /v2/fundlimit": [
        _Resp(env, status=400), _Resp(env, status=400)]})
    with pytest.raises(DhanAPIError, match="DH-906"):
        _client(http).get_balances()
    assert len(http.calls) == 2, "retry once, then surface"


def test_invalid_token_message_without_code_also_retries() -> None:
    http = _FakeHttp({"GET /v2/fundlimit": [
        _Resp({"errorMessage": "Invalid Token"}, status=400),
        _Resp({"availabelBalance": 1.0, "utilizedAmount": 0.0}),
    ]})
    _client(http).get_balances()
    assert len(http.calls) == 2


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


def test_is_invalid_token_error_classifies() -> None:
    # DH-906 (any case) and the bare "Invalid Token" message → True.
    assert is_invalid_token_error(DhanAPIError("DH-906", "Invalid Token")) is True
    assert is_invalid_token_error(DhanAPIError("dh-906", "x")) is True
    assert is_invalid_token_error(DhanAPIError("DH-XXX", "Invalid Token")) is True
    # A different Dhan error, or a non-Dhan exception → False (pages normally).
    assert is_invalid_token_error(DhanAPIError("DH-905", "Bad request")) is False
    assert is_invalid_token_error(ValueError("boom")) is False


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


# ── transient upstream vs. a real fault (2026-08-10) ─────────────────────
# The three safety sweeps paged instantly on a Dhan 502 that cleared on the
# very next 60s tick (2026-08-09, 08:20 IST). Alert fatigue on the safety path
# is its own hazard — the page you ignore is the page that mattered.
def test_5xx_is_transient() -> None:
    for code in ("500", "502", "503", "504", "599"):
        assert is_transient_upstream_error(DhanAPIError(code, "upstream")) is True


def test_dropped_connections_and_timeouts_are_transient() -> None:
    import httpx

    req = httpx.Request("GET", "https://api.dhan.co/v2/positions")
    assert is_transient_upstream_error(httpx.ConnectTimeout("t", request=req)) is True
    assert is_transient_upstream_error(httpx.ReadTimeout("t", request=req)) is True
    assert is_transient_upstream_error(httpx.ConnectError("c", request=req)) is True


def test_4xx_is_not_transient() -> None:
    """A 4xx is the bot sending something wrong. Waiting cannot fix it, so it
    must keep paging on the first failure."""
    for code in ("400", "401", "403", "404", "429"):
        assert is_transient_upstream_error(DhanAPIError(code, "client")) is False


def test_dh906_is_not_classed_as_transient() -> None:
    """It self-heals on a KNOWN schedule and has its own longer grace; the
    caller checks is_invalid_token_error first and must not double-classify."""
    assert is_transient_upstream_error(DhanAPIError("DH-906", "Invalid Token")) is False


def test_an_ordinary_bug_is_not_transient() -> None:
    """A KeyError in our own sweep is not upstream weather — page immediately."""
    assert is_transient_upstream_error(KeyError("boom")) is False
    assert is_transient_upstream_error(ValueError("boom")) is False


# ── holdings: the blind spot that made swing-indian unable to exit ────────
# Dhan splits a delivery trade across two endpoints by TIME:
#   /v2/positions  "all open positions FOR THE DAY"
#   /v2/holdings   "bought/sold in PREVIOUS trading sessions"
# The bot read only positions, so every swing-indian position — a strategy that
# holds for DAYS by design — went invisible the morning after it opened. On
# 2026-08-13 PIIND (15 @ 2514.50) had no stop, no exit and no alarm, because the
# reconciler had concluded it was closed.
_HOLDING = {
    "tradingSymbol": "PIIND", "securityId": "24184", "exchange": "NSE",
    "totalQty": 15, "availableQty": 15, "collateralQty": 15, "t1Qty": 0,
    "avgCostPrice": 2514.5,
}


def _pos(symbol="PIIND", net=15, avg=2500.0):
    return {"tradingSymbol": symbol, "netQty": net, "buyAvg": avg,
            "productType": "MTF", "positionType": "LONG"}


def test_settled_holding_appears_as_a_position() -> None:
    http = _FakeHttp({"GET /v2/positions": [_Resp([])],
                      "GET /v2/holdings": [_Resp([_HOLDING])]})
    got = _client(http).get_positions()
    assert len(got) == 1
    assert got[0].symbol == "PIIND"
    assert got[0].side == "long"
    assert got[0].size == Decimal("15")
    assert got[0].raw["_source"] == "holdings"


def test_holding_uses_available_not_total_qty() -> None:
    """Dhan defines availableQty as 'quantity available for transaction'.
    totalQty can include stock that is not sellable, and sizing a reduce-only
    sell off it would try to sell more than we can deliver."""
    h = {**_HOLDING, "totalQty": 40, "availableQty": 15}
    http = _FakeHttp({"GET /v2/positions": [_Resp([])],
                      "GET /v2/holdings": [_Resp([h])]})
    assert _client(http).get_positions()[0].size == Decimal("15")


def test_zero_available_holding_is_skipped() -> None:
    h = {**_HOLDING, "availableQty": 0}
    http = _FakeHttp({"GET /v2/positions": [_Resp([])],
                      "GET /v2/holdings": [_Resp([h])]})
    assert _client(http).get_positions() == []


def test_position_wins_over_holding_for_the_same_symbol() -> None:
    """The endpoints are disjoint by Dhan's definition (today vs previous
    sessions), but if a symbol ever appeared in both, counting it twice would
    size a reduce-only sell at 2x and could flip the account short."""
    http = _FakeHttp({"GET /v2/positions": [_Resp([_pos()])],
                      "GET /v2/holdings": [_Resp([_HOLDING])]})
    got = _client(http).get_positions()
    assert len(got) == 1
    assert got[0].raw.get("_source") is None  # the positions row, not holdings
    assert got[0].entry_price == Decimal("2500.0")


def test_positions_and_holdings_of_different_symbols_both_appear() -> None:
    http = _FakeHttp({"GET /v2/positions": [_Resp([_pos(symbol="SUZLON")])],
                      "GET /v2/holdings": [_Resp([_HOLDING])]})
    got = {p.symbol for p in _client(http).get_positions()}
    assert got == {"SUZLON", "PIIND"}


def test_holdings_failure_degrades_to_positions_only() -> None:
    """Fail-soft: losing the holdings leg returns the pre-2026-08-14 view.
    Degrading to the old blind spot beats failing the whole sweep, which would
    protect nothing at all."""
    http = _FakeHttp({"GET /v2/positions": [_Resp([_pos(symbol="SUZLON")])],
                      "GET /v2/holdings": [_Resp({"x": 1}, status=500)]})
    got = _client(http).get_positions()
    assert [p.symbol for p in got] == ["SUZLON"]


# ── correlationId "NA": the bot must not adopt the user's orders ──────────
def _order_row(corr, trig=159.0):
    return {"orderId": "1", "correlationId": corr, "tradingSymbol": "PIIND",
            "transactionType": "SELL", "quantity": 1, "filledQty": 0,
            "orderType": "STOP_LOSS_MARKET", "orderStatus": "PENDING",
            "triggerPrice": trig, "createTime": "2026-08-14 09:15:05"}


def test_dhan_na_correlation_id_is_not_ours() -> None:
    """Dhan returns the literal string "NA" for an order placed without a
    correlation id. bool("NA") is True, so the naive check adopted the user's
    hand-placed PIIND stop on 2026-08-14 — and plan_stop_protection CANCELS
    what it thinks is its own (Decision 027)."""
    http = _FakeHttp({"GET /v2/orders": [_Resp([_order_row("NA")])]})
    assert _client(http).get_open_orders()[0].reduce_only is False


def test_our_own_correlation_id_is_recognised() -> None:
    http = _FakeHttp({"GET /v2/orders": [_Resp([_order_row("abc123def")])]})
    assert _client(http).get_open_orders()[0].reduce_only is True


def test_other_absent_id_spellings_are_not_ours() -> None:
    for corr in ("", None, "na", "None", "null", "0", "  NA  "):
        http = _FakeHttp({"GET /v2/orders": [_Resp([_order_row(corr)])]})
        got = _client(http).get_open_orders()[0].reduce_only
        assert got is False, f"{corr!r} must not read as the bot's own order"


def test_a_plain_order_with_our_id_is_still_not_a_stop() -> None:
    http = _FakeHttp({"GET /v2/orders": [_Resp([_order_row("abc123", trig=0)])]})
    assert _client(http).get_open_orders()[0].reduce_only is False


# ── Per-contract lot / tick / freeze (Decision 036) ──────────────────────
_FNO_SYMBOL = "NIFTY-20260929-23150-CE"
_FUT_SYMBOL = "SBIN-20260929-FUT"

_SPECS = {
    # NIFTY option: 65 per lot, Rs 0.05 grid, 1,756 freeze.
    _FNO_SYMBOL: ContractSpec(
        lot_size=Decimal("65"), tick_size=Decimal("0.05"),
        freeze_qty=Decimal("1756"),
    ),
    # A stock future on the Rs 0.50 grid — 39 NSE contracts tick this coarsely,
    # and the pre-036 hardcoded Rs 0.05 snap produces an off-tick refusal.
    _FUT_SYMBOL: ContractSpec(
        lot_size=Decimal("750"), tick_size=Decimal("0.50"),
        freeze_qty=Decimal("15000"),
    ),
}


def _fno_resolve(symbol: str) -> tuple[str, str]:
    if symbol in _SPECS:
        return ("9001", "NSE_FNO")
    return _UNIVERSE[symbol]


def _fno_client(http: _FakeHttp) -> DhanClient:
    return DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"), client_id="C1",
        resolve_symbol=_fno_resolve, base_url="https://sandbox.dhan.co",
        product_type="MARGIN", mtf_fallback_cnc=False, http=http,
        contract_spec=_SPECS.get,
    )


def test_stop_trigger_snaps_to_the_contracts_own_tick() -> None:
    """The bug this fixes: Rs 0.05 is not a multiple of Rs 0.50, so the old
    hardcoded snap produced a trigger the venue refuses outright."""
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"orderId": "1", "orderStatus": "TRANSIT"})]})
    req = OrderRequest(symbol=_FUT_SYMBOL, side="sell", size=Decimal("750"),
                       order_type=OrderType.MARKET, stop_price=Decimal("812.37"),
                       reduce_only=True)
    _fno_client(http).place_order(req)
    trigger = Decimal(str(http.calls[0]["json"]["triggerPrice"]))
    assert trigger % Decimal("0.50") == 0
    assert trigger == Decimal("812.50")


def test_cash_equity_tick_is_unchanged_without_a_spec() -> None:
    """Every pre-036 caller passes no contract_spec and must behave identically."""
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"orderId": "1", "orderStatus": "TRANSIT"})]})
    req = OrderRequest(symbol="SWIGGY", side="sell", size=Decimal("10"),
                       order_type=OrderType.MARKET, stop_price=Decimal("412.37"),
                       reduce_only=True)
    _client(http).place_order(req)
    assert http.calls[0]["json"]["triggerPrice"] == 412.35


def test_contract_size_returns_the_lot_for_derivatives() -> None:
    client = _fno_client(_FakeHttp({}))
    assert client.contract_size(_FNO_SYMBOL) == Decimal("65")
    # Cash equity stays one share per unit.
    assert client.contract_size("SWIGGY") == Decimal("1")


def test_freeze_quantity_refuses_an_oversized_order() -> None:
    """Refusing beats clamping: a clamped entry opens a smaller position than
    the allocator sized and the stop was computed for."""
    http = _FakeHttp({})
    req = OrderRequest(symbol=_FNO_SYMBOL, side="buy", size=Decimal("1820"),
                       order_type=OrderType.MARKET)
    with pytest.raises(DhanAPIError, match="freeze quantity"):
        _fno_client(http).place_order(req)
    assert not http.calls, "an over-freeze order must never reach the venue"


def test_order_at_the_freeze_limit_is_allowed() -> None:
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"orderId": "1", "orderStatus": "TRANSIT"})]})
    req = OrderRequest(symbol=_FNO_SYMBOL, side="buy", size=Decimal("1756"),
                       order_type=OrderType.MARKET)
    _fno_client(http).place_order(req)
    assert http.calls[0]["json"]["quantity"] == 1756


def test_cash_equity_has_no_freeze_cap() -> None:
    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"orderId": "1", "orderStatus": "TRANSIT"})]})
    req = OrderRequest(symbol="SWIGGY", side="buy", size=Decimal("999999"),
                       order_type=OrderType.MARKET)
    _client(http).place_order(req)
    assert http.calls[0]["json"]["quantity"] == 999999


def test_a_failing_spec_lookup_degrades_to_the_cash_defaults() -> None:
    """Fail-soft: a stale cache must not take down an order path that worked
    on the fallback constants before this existed."""
    def _boom(symbol: str) -> ContractSpec | None:
        raise RuntimeError("registry unavailable")

    http = _FakeHttp({"POST /v2/orders": [
        _Resp({"orderId": "1", "orderStatus": "TRANSIT"})]})
    client = DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"), client_id="C1",
        resolve_symbol=_fno_resolve, base_url="https://sandbox.dhan.co",
        http=http, contract_spec=_boom,
    )
    req = OrderRequest(symbol=_FUT_SYMBOL, side="sell", size=Decimal("750"),
                       order_type=OrderType.MARKET, stop_price=Decimal("812.37"),
                       reduce_only=True)
    client.place_order(req)
    assert http.calls[0]["json"]["triggerPrice"] == 812.35  # Rs 0.05 fallback


# ---------------------------------------------------------------------------
# Rejection reasons — Phase 11c
#
# August 2026: 588 REJECTED orders against 9 FILLED, and not one recorded why.
# Dhan's RMS rejects ASYNCHRONOUSLY — the POST returns 2xx with TRANSIT and the
# verdict lands in the order book afterwards, under `omsErrorDescription`. A
# place_order that reported the POST's status could never see it, so an
# MTF-ineligible scrip and an out-of-band stop trigger were the same fact:
# "rejected", retried every ~90s forever.
# ---------------------------------------------------------------------------
def test_async_rejection_is_read_off_the_order_book() -> None:
    http = _FakeHttp({
        "POST /v2/orders": [_Resp({"orderId": "901", "orderStatus": "TRANSIT"})],
        "GET /v2/orders/901": [
            _Resp({"orderStatus": "REJECTED",
                   "omsErrorDescription": "MTF is not permitted for this Scrip"})],
    })
    req = OrderRequest(symbol="TBZ", side="buy", size=Decimal("19"))
    res = _client(http, fallback=False).place_order(req)

    assert res.status == "rejected"  # NOT the POST's "pending"
    assert res.raw["_reject_reason"] == "MTF is not permitted for this Scrip"


def test_verify_polls_while_the_venue_is_undecided() -> None:
    """TRANSIT is not a verdict — keep reading until the book has one."""
    http = _FakeHttp({
        "POST /v2/orders": [_Resp({"orderId": "902", "orderStatus": "TRANSIT"})],
        "GET /v2/orders/902": [
            _Resp({"orderStatus": "TRANSIT"}),
            _Resp({"orderStatus": "PENDING"}),
            _Resp({"orderStatus": "REJECTED",
                   "omsErrorDescription": "Insufficient margin"})],
    })
    res = _client(http, fallback=False).place_order(
        OrderRequest(symbol="TBZ", side="buy", size=Decimal("19"))
    )
    assert res.raw["_reject_reason"] == "Insufficient margin"


def test_a_settled_live_order_stops_polling_immediately() -> None:
    """The common case must cost ONE read and no sleep."""
    http = _FakeHttp({
        "POST /v2/orders": [_Resp({"orderId": "903", "orderStatus": "TRANSIT"})],
        "GET /v2/orders/903": [_Resp({"orderStatus": "TRADED"})],
    })
    res = _client(http, fallback=False).place_order(
        OrderRequest(symbol="TBZ", side="buy", size=Decimal("19"))
    )
    assert res.status == "filled"
    assert "_reject_reason" not in res.raw
    assert len([c for c in http.calls if c["method"] == "GET"]) == 1


def test_a_rejection_with_no_text_still_reports_something() -> None:
    """A blank omsErrorDescription must not read as 'no error'."""
    http = _FakeHttp({
        "POST /v2/orders": [_Resp({"orderId": "904", "orderStatus": "TRANSIT"})],
        "GET /v2/orders/904": [_Resp({"orderStatus": "REJECTED"})],
    })
    res = _client(http, fallback=False).place_order(
        OrderRequest(symbol="TBZ", side="buy", size=Decimal("19"))
    )
    assert res.raw["_reject_reason"] == "REJECTED"


def test_a_failed_verify_leaves_the_post_status_standing() -> None:
    """Best-effort: the order IS placed, and the reconciler settles it later."""
    http = _FakeHttp({
        "POST /v2/orders": [_Resp({"orderId": "905", "orderStatus": "TRANSIT"})],
    })  # no GET route => the verify raises internally
    res = _client(http, fallback=False).place_order(
        OrderRequest(symbol="TBZ", side="buy", size=Decimal("19"))
    )
    assert res.status == "pending"
    assert res.exchange_order_id == "905"
    assert "_reject_reason" not in res.raw


def test_get_order_surfaces_the_reason_for_the_reconciler() -> None:
    """The path that matters most: an async rejection found minutes later."""
    http = _FakeHttp({
        "GET /v2/orders/906": [
            _Resp({"orderId": "906", "orderStatus": "REJECTED", "quantity": 19,
                   "transactionType": "SELL", "tradingSymbol": "PIIND",
                   "omsErrorDescription": "Trigger price out of range"})],
    })
    order = _client(http).get_order("906")
    assert order is not None
    assert order.status == "rejected"
    assert order.reject_reason == "Trigger price out of range"


def test_a_live_order_carries_no_reject_reason() -> None:
    http = _FakeHttp({
        "GET /v2/orders/907": [
            _Resp({"orderId": "907", "orderStatus": "OPEN", "quantity": 19,
                   "transactionType": "BUY", "tradingSymbol": "PIIND"})],
    })
    order = _client(http).get_order("907")
    assert order is not None
    assert order.reject_reason is None
