"""Dhan charts fetch survives the 5 req/s Data-API cap.

Regression for 2026-07-22: the intraday scan fired ~2 calls/symbol across the
NIFTY-100 flat out; 95/99 came back 429 and the morning cut read almost-empty.
"""

from __future__ import annotations

import httpx
import pytest

from src.data_sources.dhan import (
    _CHARTS_MAX_ATTEMPTS,
    DhanData,
    _retry_after_seconds,
)


class _StubToken:
    def token(self) -> str:
        return "tok"

    def invalidate(self) -> None:  # pragma: no cover - not exercised here
        pass


def _client(handler: httpx.MockTransport, delay: float = 0.0) -> DhanData:
    return DhanData(
        token_manager=_StubToken(),  # type: ignore[arg-type]
        http=httpx.Client(transport=handler),
        universe={"X": {"security_id": "1", "exchange": "NSE_EQ"}},
        request_delay_seconds=delay,
    )


_OHLC = {
    "timestamp": [1_700_000_000],
    "open": [100.0], "high": [101.0], "low": [99.0],
    "close": [100.5], "volume": [1000],
}


def test_retries_then_succeeds_after_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.data_sources.dhan.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=_OHLC)

    bars = _client(httpx.MockTransport(handler)).get_ohlcv("X", "5m")
    assert calls["n"] == 3  # two 429s, then a 200
    assert len(bars) == 1


def test_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.data_sources.dhan.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429)

    with pytest.raises(httpx.HTTPStatusError):
        _client(httpx.MockTransport(handler)).get_ohlcv("X", "5m")
    assert calls["n"] == _CHARTS_MAX_ATTEMPTS  # bounded, no infinite loop


def test_retry_after_header_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "src.data_sources.dhan.time.sleep", lambda s: slept.append(s)
    )
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=_OHLC)

    _client(httpx.MockTransport(handler)).get_ohlcv("X", "5m")
    assert 3.0 in slept, "Retry-After value must be used verbatim"


def test_pacing_delay_applied_before_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "src.data_sources.dhan.time.sleep", lambda s: slept.append(s)
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OHLC)

    _client(httpx.MockTransport(handler), delay=0.22).get_ohlcv("X", "5m")
    assert slept == [0.22], "one pace delay before the single successful call"


def test_no_pacing_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "src.data_sources.dhan.time.sleep", lambda s: slept.append(s)
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OHLC)

    _client(httpx.MockTransport(handler)).get_ohlcv("X", "5m")
    assert slept == [], "default client must not pace (prepare job, tests)"


def test_retry_after_parser() -> None:
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "5"})) == 5.0
    assert _retry_after_seconds(httpx.Response(429)) is None
    assert _retry_after_seconds(
        httpx.Response(429, headers={"Retry-After": "notanumber"})
    ) is None
