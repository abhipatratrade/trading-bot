"""Dhan market-data adapter — OHLCV + quote parsing (Phase 3/4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.brokers.dhan.auth import DhanTokenManager
from src.data_sources.dhan import DhanData

_UNIVERSE = {
    "SWIGGY": {"security_id": "1001", "exchange": "NSE_EQ"},
    "TBZ": {"security_id": "2002", "exchange": "NSE_EQ"},
}


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    """Routes POSTs by URL suffix to queued responses; records calls."""

    def __init__(self, routes: dict[str, list[_Resp]]) -> None:
        self._routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[dict] = []

    def post(self, url: str, json: dict | None = None, headers: dict | None = None) -> _Resp:
        self.calls.append({"url": url, "json": json, "headers": headers})
        for suffix, resps in self._routes.items():
            if url.endswith(suffix):
                return resps.pop(0)
        raise AssertionError(f"unexpected URL {url}")


def _data(http: _FakeHttp) -> DhanData:
    token = DhanTokenManager(static_token="TOK")  # static → no refresh calls
    return DhanData(token_manager=token, universe=_UNIVERSE, http=http)


def test_get_ohlcv_daily_parses_and_sorts() -> None:
    # Return two bars out of order to prove sorting.
    payload = {
        "timestamp": [1_700_000_000, 1_699_900_000],
        "open": [10, 9], "high": [11, 9.5], "low": [9.5, 8.5],
        "close": [10.5, 9], "volume": [1000, 900],
    }
    http = _FakeHttp({"/v2/charts/historical": [_Resp(payload)]})
    bars = _data(http).get_ohlcv("SWIGGY", "1d", limit=10)
    assert [b.close for b in bars] == [Decimal("9"), Decimal("10.5")]
    assert bars[0].timestamp < bars[1].timestamp
    # correct security id + exchange in the request body
    body = http.calls[0]["json"]
    assert body["securityId"] == "1001"
    assert body["exchangeSegment"] == "NSE_EQ"


def test_get_ohlcv_intraday_uses_minutes() -> None:
    payload = {"timestamp": [1_700_000_000], "open": [1], "high": [2],
               "low": [1], "close": [2], "volume": [5]}
    http = _FakeHttp({"/v2/charts/intraday": [_Resp(payload)]})
    _data(http).get_ohlcv("SWIGGY", "15m")
    assert http.calls[0]["json"]["interval"] == "15"


def test_get_ohlcv_empty_timestamp_returns_empty() -> None:
    http = _FakeHttp({"/v2/charts/historical": [_Resp({"timestamp": None})]})
    assert _data(http).get_ohlcv("SWIGGY", "1d") == []


def test_get_ohlcv_unknown_symbol_raises() -> None:
    http = _FakeHttp({})
    with pytest.raises(ValueError, match="Unknown Dhan symbol"):
        _data(http).get_ohlcv("NOPE", "1d")


def test_get_ohlcv_bad_interval_raises() -> None:
    http = _FakeHttp({})
    with pytest.raises(ValueError, match="Unsupported Dhan interval"):
        _data(http).get_ohlcv("SWIGGY", "3d")


def test_401_triggers_token_invalidate_and_retry() -> None:
    payload = {"timestamp": [1_700_000_000], "open": [1], "high": [1],
               "low": [1], "close": [1], "volume": [1]}
    http = _FakeHttp({"/v2/charts/historical": [_Resp({}, status=401),
                                                _Resp(payload)]})
    # refreshable token so invalidate() actually clears + re-mints
    calls = {"n": 0}

    class _TokHttp:
        def post(self, url, params=None):
            calls["n"] += 1
            import base64 as b64
            import json as j
            raw = j.dumps({"exp": 9_999_999_999}).encode()
            exp = b64.urlsafe_b64encode(raw).decode().rstrip("=")
            return _Resp({"accessToken": f"h.{exp}.s"})

    token = DhanTokenManager(client_id="c", pin="p", totp_secret="JBSWY3DPEHPK3PXP",
                             http=_TokHttp())
    d = DhanData(token_manager=token, universe=_UNIVERSE, http=http)
    bars = d.get_ohlcv("SWIGGY", "1d")
    assert len(bars) == 1
    assert len(http.calls) == 2       # first 401, then retried
    assert calls["n"] == 2            # token minted twice (initial + after invalidate)


def test_get_ticker_parses_quote() -> None:
    quote = {"data": {"NSE_EQ": {"1001": {
        "last_price": 281.5, "volume": 12345,
        "ohlc": {"open": 279, "high": 283, "low": 278, "close": 280},
    }}}}
    http = _FakeHttp({"/v2/marketfeed/quote": [_Resp(quote)]})
    t = _data(http).get_ticker("SWIGGY")
    assert t.last_price == Decimal("281.5")
    assert t.volume_24h == Decimal("12345")
    assert t.raw["prev_close"] == 280


def test_get_funding_rate_not_supported() -> None:
    with pytest.raises(NotImplementedError):
        _data(_FakeHttp({})).get_funding_rate("SWIGGY")
