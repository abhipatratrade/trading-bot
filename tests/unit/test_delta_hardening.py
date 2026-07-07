"""Delta client hardening: retries, 429, clock skew, catalogue TTL."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from src.brokers.delta_india.client import DeltaAPIError, DeltaIndiaClient
from src.core.logging import get_logger


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:  # pragma: no cover
        pass


class FakeHttp:
    """Yields scripted responses; raising entries are raised instead."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_: Any) -> FakeResponse:
        self.calls.append((method, url))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(script: list[Any]) -> DeltaIndiaClient:
    c = DeltaIndiaClient.__new__(DeltaIndiaClient)
    c._log = get_logger("test")
    c._api_key = "k"
    c._api_secret = "s"
    c._http = FakeHttp(script)  # type: ignore[assignment]
    c._products = None
    c._products_fetched_at = 0.0
    c._time_offset = 0.0
    return c


OK = FakeResponse({"success": True, "result": {"ok": 1}})


def test_get_retries_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    client = _client([httpx.ConnectError("boom"), OK])
    assert client._get("/v2/wallet/balances") == {"ok": 1}
    assert len(client._http.calls) == 2  # type: ignore[attr-defined]


def test_post_does_not_retry_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    client = _client([httpx.ConnectError("boom"), OK])
    with pytest.raises(httpx.ConnectError):
        client._post("/v2/orders", {"x": 1})
    assert len(client._http.calls) == 1  # type: ignore[attr-defined]


def test_429_is_retried_even_for_post(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    limited = FakeResponse({}, status_code=429, headers={"Retry-After": "1"})
    client = _client([limited, OK])
    assert client._post("/v2/orders", {"x": 1}) == {"ok": 1}
    assert slept and slept[0] <= 10.0


def test_expired_signature_resyncs_clock_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    rejected = FakeResponse(
        {"success": False, "error": {"code": "expired_signature"}},
        headers={"Date": "Tue, 07 Jul 2026 12:00:00 GMT"},
    )
    client = _client([rejected, OK])
    assert client._get("/v2/positions/margined") == {"ok": 1}
    assert client._time_offset != 0.0  # learned from the Date header


def test_persistent_api_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    bad = FakeResponse({"success": False, "error": {"code": "invalid_api_key"}})
    client = _client([bad])
    with pytest.raises(DeltaAPIError):
        client._get("/v2/wallet/balances")


def test_products_cache_refreshes_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    products = FakeResponse(
        {"success": True, "result": [{"symbol": "BTCUSD", "id": 1}]}
    )
    client = _client([products, products])
    client._ensure_products()
    client._ensure_products()  # fresh → no second call
    assert len(client._http.calls) == 1  # type: ignore[attr-defined]
    client._products_fetched_at -= client._PRODUCTS_TTL_SECONDS + 1
    client._ensure_products()  # stale → refetch
    assert len(client._http.calls) == 2  # type: ignore[attr-defined]


def test_stale_products_survive_failed_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    products = FakeResponse(
        {"success": True, "result": [{"symbol": "BTCUSD", "id": 1}]}
    )
    # Refresh attempt: all three GET retries fail with transport errors.
    client = _client(
        [products]
        + [httpx.ConnectError("down")] * 3
    )
    client._ensure_products()
    client._products_fetched_at -= client._PRODUCTS_TTL_SECONDS + 1
    client._ensure_products()  # refresh fails → stale catalogue kept
    assert client._products is not None
    assert "BTCUSD" in client._products
