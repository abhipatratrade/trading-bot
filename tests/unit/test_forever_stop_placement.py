"""Decision 035 — a protective stop that survives the night (Phase 12a).

swing-indian carries MTF for days. Its stop was a DAY order, expired by Dhan at
15:30, so the book was naked every night from 2026-08-18 (found) to 09-16
(built), and on 09-12 01:34 `stop_coverage` halted the bucket over a stop
nothing could place. These pin the placement path, the retire-on-close guard
that makes a fails-open order safe to rest, and the master switch that keeps
all of it dark until NSE_EQ + MTF is proven at the venue.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.brokers.base import OrderRequest, OrderType
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import AttachedStopRetireError, DhanClient
from src.core.models import OrderStatus
from src.safety import session_invariants as si
from src.safety import stop_protection as sp

_UNIVERSE = {"KEI": ("1001", "NSE_EQ"), "COCHINSHIP": ("21508", "NSE_EQ")}


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

    def paths(self, method: str) -> list[str]:
        return [c["url"].split(".co", 1)[1] for c in self.calls if c["method"] == method]


def _client(
    http: _FakeHttp, *, forever_stops: bool = False, ledger: set[str] | None = None
) -> DhanClient:
    c = DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=_resolve,
        base_url="https://api.dhan.co",
        product_type="MTF",
        http=http,
        owns_order_id=(lambda oid: oid in ledger) if ledger is not None else None,
    )
    c.forever_stops = forever_stops
    return c


def _stop(*, forever: bool, trigger: str = "4400.00") -> OrderRequest:
    return OrderRequest(
        symbol="KEI",
        side="sell",
        size=Decimal("7"),
        order_type=OrderType.MARKET,
        reduce_only=True,
        stop_price=Decimal(trigger),
        product="MTF",
        client_order_id="stop-4400-7-202609161510",
        forever=forever,
    )


def _gtt(order_id: str, *, correlation: str | None, symbol: str = "KEI") -> dict:
    body = {
        "orderId": order_id,
        "orderStatus": "PENDING",
        "transactionType": "SELL",
        "exchangeSegment": "NSE_EQ",
        "productType": "MTF",
        "orderType": "LIMIT",
        "tradingSymbol": symbol,
        "securityId": _UNIVERSE[symbol][0],
        "quantity": 7,
        "triggerPrice": 4400.0,
        "price": 4356.0,
    }
    if correlation is not None:
        body["correlationId"] = correlation
    return body


# ── placement ───────────────────────────────────────────────────────────


def test_a_forever_stop_goes_to_the_gtt_book_in_the_proven_shape() -> None:
    http = _FakeHttp({
        "POST /v2/forever/orders": [_Resp({"orderId": "GTT9", "orderStatus": "PENDING"})],
    })
    res = _client(http).place_order(_stop(forever=True))

    assert http.paths("POST") == ["/v2/forever/orders"]
    body = http.calls[0]["json"]
    assert body["orderFlag"] == "SINGLE"
    assert body["transactionType"] == "SELL"
    assert body["exchangeSegment"] == "NSE_EQ"
    assert body["productType"] == "MTF"
    assert body["orderType"] == "LIMIT"  # the GTT vocabulary has no SL-M
    assert body["triggerPrice"] == 4400.0
    assert body["price"] == 4356.0  # 1% THROUGH the trigger, snapped to tick
    assert body["quantity"] == 7
    assert body["correlationId"] == "stop-4400-7-202609161510"
    assert res.status == "open"
    assert res.exchange_order_id == "GTT9"
    assert res.raw["_forever"] is True


def test_the_new_gtt_is_routable_for_cancel_immediately() -> None:
    http = _FakeHttp({"POST /v2/forever/orders": [_Resp({"orderId": "GTT9"})]})
    c = _client(http)
    c.place_order(_stop(forever=True))
    assert "GTT9" in c._forever_ids


def test_without_the_flag_a_stop_is_still_a_day_order() -> None:
    """The pre-035 path, byte for byte: /v2/orders, STOP_LOSS_MARKET, DAY."""
    http = _FakeHttp({
        "POST /v2/orders": [_Resp({"orderId": "555", "orderStatus": "PENDING"})],
        "GET /v2/orders/555": [_Resp({"orderStatus": "PENDING"})] * 4,
    })
    _client(http).place_order(_stop(forever=False))
    assert http.paths("POST") == ["/v2/orders"]
    body = http.calls[0]["json"]
    assert body["orderType"] == "STOP_LOSS_MARKET" and body["validity"] == "DAY"


def test_forever_without_a_trigger_is_not_a_forever_order() -> None:
    """The flag only means something on a stop. A plain sell stays plain."""
    http = _FakeHttp({
        "GET /v2/super/orders": [_Resp([])],
        "POST /v2/orders": [_Resp({"orderId": "556", "orderStatus": "TRADED"})],
        "GET /v2/orders/556": [_Resp({"orderStatus": "TRADED"})],
    })
    req = OrderRequest(symbol="KEI", side="sell", size=Decimal("7"),
                       order_type=OrderType.MARKET, reduce_only=True, forever=True)
    _client(http).place_order(req)
    assert http.paths("POST") == ["/v2/orders"]


def test_a_buy_stop_sets_its_limit_above_the_trigger() -> None:
    http = _FakeHttp({"POST /v2/forever/orders": [_Resp({"orderId": "G"})]})
    req = OrderRequest(symbol="KEI", side="buy", size=Decimal("7"), order_type=OrderType.MARKET,
                       reduce_only=True, stop_price=Decimal("5000"), product="MTF", forever=True)
    _client(http).place_order(req)
    body = http.calls[0]["json"]
    assert body["triggerPrice"] == 5000.0 and body["price"] == 5050.0


# ── retire on close: the fails-open prerequisite ────────────────────────


def _close() -> OrderRequest:
    return OrderRequest(symbol="KEI", side="sell", size=Decimal("7"),
                        order_type=OrderType.MARKET, reduce_only=True)


def test_a_close_retires_our_resting_gtt_first() -> None:
    http = _FakeHttp({
        "GET /v2/super/orders": [_Resp([])],
        "GET /v2/forever/orders": [_Resp([_gtt("GTT1", correlation="stop-4400-7-202609161510")])],
        "DELETE /v2/forever/orders/GTT1": [_Resp({"orderStatus": "CANCELLED"})],
        "POST /v2/orders": [_Resp({"orderId": "777", "orderStatus": "TRADED"})],
        "GET /v2/orders/777": [_Resp({"orderStatus": "TRADED"})],
    })
    _client(http, forever_stops=True).place_order(_close())
    methods = [(c["method"], c["url"].split(".co", 1)[1]) for c in http.calls]
    # look up, cancel, only THEN sell
    cancel_at = methods.index(("DELETE", "/v2/forever/orders/GTT1"))
    sell_at = methods.index(("POST", "/v2/orders"))
    assert cancel_at < sell_at


def test_a_close_retires_a_gtt_only_the_ledger_can_prove() -> None:
    """COCHINSHIP, 2026-09-18: sold, and its 14 GTT sells kept resting with no
    position behind them, because Dhan's GTT list omits correlationId and the
    retire step could not claim a single one."""
    http = _FakeHttp({
        "GET /v2/super/orders": [_Resp([])],
        "GET /v2/forever/orders": [_Resp([_gtt("GTT1", correlation=None)])],
        "DELETE /v2/forever/orders/GTT1": [_Resp({"orderStatus": "CANCELLED"})],
        "POST /v2/orders": [_Resp({"orderId": "777", "orderStatus": "TRADED"})],
        "GET /v2/orders/777": [_Resp({"orderStatus": "TRADED"})],
    })
    _client(http, forever_stops=True, ledger={"GTT1"}).place_order(_close())
    assert http.paths("DELETE") == ["/v2/forever/orders/GTT1"]


def test_the_users_gtt_on_the_same_scrip_is_left_resting() -> None:
    """Decision 027: no correlationId of ours, not ours, not touched."""
    http = _FakeHttp({
        "GET /v2/super/orders": [_Resp([])],
        "GET /v2/forever/orders": [_Resp([_gtt("USER1", correlation=None)])],
        "POST /v2/orders": [_Resp({"orderId": "777", "orderStatus": "TRADED"})],
        "GET /v2/orders/777": [_Resp({"orderStatus": "TRADED"})],
    })
    _client(http, forever_stops=True).place_order(_close())
    assert http.paths("DELETE") == []


def test_a_gtt_that_cannot_be_retired_aborts_the_close() -> None:
    """Refusing to sell is recoverable. Selling, then being sold again by a
    stop that outlived the position, is not."""
    http = _FakeHttp({
        "GET /v2/super/orders": [_Resp([])],
        "GET /v2/forever/orders": [_Resp([_gtt("GTT1", correlation="stop-4400-7-202609161510")])],
        "DELETE /v2/forever/orders/GTT1": [_Resp({"errorCode": "DH-101", "errorMessage": "nope"})],
    })
    with pytest.raises(AttachedStopRetireError):
        _client(http, forever_stops=True).place_order(_close())
    assert http.paths("POST") == []


def test_a_failed_gtt_lookup_aborts_the_close() -> None:
    """An empty answer is indistinguishable from 'nothing resting'."""
    http = _FakeHttp({
        "GET /v2/super/orders": [_Resp([])],
        "GET /v2/forever/orders": [_Resp({"errorCode": "500", "errorMessage": "down"})],
    })
    with pytest.raises(AttachedStopRetireError):
        _client(http, forever_stops=True).place_order(_close())
    assert http.paths("POST") == []


def test_with_the_switch_off_a_close_never_reads_the_gtt_book() -> None:
    """Quota: the account returned 805 on 2026-08-31. Dark means dark."""
    http = _FakeHttp({
        "GET /v2/super/orders": [_Resp([])],
        "POST /v2/orders": [_Resp({"orderId": "777", "orderStatus": "TRADED"})],
        "GET /v2/orders/777": [_Resp({"orderStatus": "TRADED"})],
    })
    _client(http, forever_stops=False).place_order(_close())
    assert "/v2/forever/orders" not in http.paths("GET")


# ── get_order answers from the right book ───────────────────────────────


def test_get_order_finds_a_resting_gtt_in_the_forever_book() -> None:
    http = _FakeHttp({
        "GET /v2/forever/orders": [_Resp([_gtt("GTT1", correlation="stop-4400-7-202609161510")])],
    })
    c = _client(http)
    c._forever_ids.add("GTT1")
    o = c.get_order("GTT1")
    assert o is not None and o.status == "open" and o.forever


def test_a_gtt_gone_from_the_book_reads_as_canceled() -> None:
    """Fired or cancelled by hand — either way it no longer rests. The fill a
    fired one produced is booked by the shortfall detector, not here."""
    http = _FakeHttp({"GET /v2/forever/orders": [_Resp([])]})
    c = _client(http)
    c._forever_ids.add("GTT1")
    o = c.get_order("GTT1")
    assert o is not None and o.status == "canceled" and o.forever


# ── the sweep asks for the overnight kind only where configured ─────────


class _OM:
    broker_name = "dhan"

    def __init__(self) -> None:
        self.placed: list[dict] = []

    def place_order(self, **kw):
        self.placed.append(kw)
        return SimpleNamespace(exchange_order_id=None, status=None)

    def cancel_order(self, **kw):  # pragma: no cover
        pass


class _Broker:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions

    def get_open_orders(self):
        return []

    def supports_attached_stop(self):
        return False

    def supports_forever_orders(self):
        return True

    def get_forever_orders(self, symbol=None):
        return []

    def tick_size(self, symbol):
        return Decimal("0.05")


def _long(symbol: str, qty: str, entry: str):
    from src.brokers.base import PositionInfo

    return PositionInfo(symbol=symbol, side="long", size=Decimal(qty), entry_price=Decimal(entry))


def _stub_ledger(monkeypatch) -> None:
    monkeypatch.setattr(sp, "_load_attribution", lambda *a, **k: {
        "KEI": ("swing-indian", "mean_reversion_1h"),
        "COFORGE": ("intraday-indian", "gap_down_reversal_broad"),
    })
    monkeypatch.setattr(sp, "_load_stop_distances", lambda *a, **k: {})
    monkeypatch.setattr(sp, "_load_entry_prices", lambda *a, **k: {})
    monkeypatch.setattr(sp, "_load_recent_entry_symbols", lambda *a, **k: set())
    monkeypatch.setattr(
        sp, "bot_owned_quantities",
        lambda *a, **k: {"KEI": Decimal("7"), "COFORGE": Decimal("27")},
    )
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        yield object()

    monkeypatch.setattr(sp, "session_scope", _scope)
    monkeypatch.setattr(sp, "should_attempt_place", lambda *a, **k: True)
    monkeypatch.setattr(sp, "reset_place_failures", lambda *a, **k: None)
    monkeypatch.setattr(sp, "_last_placed", {})


@pytest.fixture
def swept(monkeypatch):
    """Drive the real sweep with the ledger lookups stubbed."""
    _stub_ledger(monkeypatch)

    def run(*, enabled: bool, by_bucket: dict[str, bool]):
        om = _OM()
        sp.ensure_stop_protection(
            account_ref="dhan",
            bucket_ids=["swing-indian", "intraday-indian"],
            broker=_Broker([_long("KEI", "7", "4883.5"), _long("COFORGE", "27", "1838.5")]),
            order_manager=om,
            stop_pct_by_bucket={"swing-indian": Decimal("20"), "intraday-indian": Decimal("15")},
            product_by_bucket={"swing-indian": "MTF", "intraday-indian": "INTRADAY"},
            shared_account=True,
            forever_stops_enabled=enabled,
            forever_by_bucket=by_bucket,
        )
        return {p["symbol"]: p["forever"] for p in om.placed}

    return run


def test_only_the_configured_bucket_gets_a_forever_stop(swept):
    got = swept(enabled=True, by_bucket={"swing-indian": True})
    assert got == {"KEI": True, "COFORGE": False}


def test_with_the_master_switch_off_nobody_does(swept):
    got = swept(enabled=False, by_bucket={"swing-indian": True})
    assert got == {"KEI": False, "COFORGE": False}


# ── the stacking guard: never place a stop on top of one you cannot see ──


class _BlindBroker(_Broker):
    """A venue that holds the bot's stops while every listing hides them —
    the 2026-09-24 shape, where the GTT list omitted correlationId."""

    def __init__(self, positions, live: set[str]):
        super().__init__(positions)
        self.live = live

    def get_order(self, oid):
        return SimpleNamespace(status="open" if oid in self.live else "canceled")


class _PlacingOM(_OM):
    def place_order(self, **kw):
        self.placed.append(kw)
        return SimpleNamespace(
            exchange_order_id=f"GTT{len(self.placed)}", status=OrderStatus.OPEN
        )


def test_a_live_stop_the_sweep_cannot_see_is_not_stacked(monkeypatch):
    _stub_ledger(monkeypatch)
    alerts: list[str] = []
    monkeypatch.setattr(sp, "send_alert_dedup", lambda key, msg: alerts.append(msg))
    om = _PlacingOM()
    live: set[str] = set()
    broker = _BlindBroker([_long("KEI", "7", "4883.5")], live)

    def tick():
        sp.ensure_stop_protection(
            account_ref="dhan",
            bucket_ids=["swing-indian"],
            broker=broker,
            order_manager=om,
            stop_pct_by_bucket={"swing-indian": Decimal("20")},
            shared_account=True,
        )

    tick()
    live.add("GTT1")  # it rests at the venue; the listing still hides it
    tick()
    tick()

    assert len(om.placed) == 1
    assert alerts and "NOT placing another" in alerts[0]

    live.clear()  # fired, or cancelled by hand: a stop is genuinely needed
    tick()
    assert len(om.placed) == 2


# ── configuration and wiring ────────────────────────────────────────────


def test_swing_indian_is_configured_for_it_and_nothing_else_is():
    from src.shared.bucket import load_buckets

    v = {b.id: b.config.stop_validity for b in load_buckets() if b.config.enabled}
    assert v == {"swing-indian": "forever", "intraday-indian": "day", "commodity-indian": "day"}


def test_it_ships_dark():
    from src.core.config import Settings

    assert Settings.model_fields["forever_stops_enabled"].default is False


def test_the_coverage_check_reads_the_gtt_book_when_enabled():
    """A resting GTT must count as coverage, or the invariant pages 'NO
    PROTECTIVE STOP' over the very order this feature rests."""
    src = inspect.getsource(si.run_session_invariants)
    assert "forever_stops_enabled" in src and "get_forever_orders" in src


def test_run_bot_threads_the_switch_everywhere():
    import pathlib

    src = pathlib.Path("src/entrypoints/run_bot.py").read_text(encoding="utf-8")
    assert src.count("forever_stops_enabled=settings.forever_stops_enabled") == 2
    assert "client.forever_stops = settings.forever_stops_enabled" in src
    assert "forever_by_bucket=stop_validities" in src
    # A venue that is shut must reach the planner as PAUSED, not merely be
    # missing from the pct map (2026-09-24: POLICYBZR at commodity's 4.5%).
    assert "paused_buckets=paused" in src
