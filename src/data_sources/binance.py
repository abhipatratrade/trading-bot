"""
Binance Futures public market data — REST + WebSocket.

No authentication required.  Used for signal generation (OHLCV, funding
rates) per Decision 004; execution goes to Delta Exchange India.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from src.core.config import Settings, get_settings
from src.core.logging import get_logger
from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker


class BinanceData(MarketData):
    """Synchronous REST client for Binance Futures public data."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._log = get_logger("data_sources.binance")
        self._http = httpx.Client(
            base_url=self._settings.binance_rest_url,
            timeout=10.0,
            headers={"User-Agent": "trading-bot/0.1.0"},
        )

    # ── MarketData interface ────────────────────────────────────────

    def get_ohlcv(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[OHLCVBar]:
        resp = self._http.get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        resp.raise_for_status()
        bars: list[OHLCVBar] = []
        for k in resp.json():
            bars.append(
                OHLCVBar(
                    timestamp=datetime.fromtimestamp(k[0] / 1000, tz=UTC),
                    open=Decimal(k[1]),
                    high=Decimal(k[2]),
                    low=Decimal(k[3]),
                    close=Decimal(k[4]),
                    volume=Decimal(k[5]),
                )
            )
        return bars

    def get_ticker(self, symbol: str) -> Ticker:
        resp = self._http.get(
            "/fapi/v1/ticker/24hr", params={"symbol": symbol}
        )
        resp.raise_for_status()
        t: dict[str, Any] = resp.json()
        return self._parse_ticker(t)

    def get_tickers(self) -> list[Ticker]:
        resp = self._http.get("/fapi/v1/ticker/24hr")
        resp.raise_for_status()
        return [self._parse_ticker(t) for t in resp.json()]

    def get_funding_rate(self, symbol: str) -> FundingRate:
        resp = self._http.get(
            "/fapi/v1/premiumIndex", params={"symbol": symbol}
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        nft = data.get("nextFundingTime")
        return FundingRate(
            symbol=data["symbol"],
            rate=Decimal(data["lastFundingRate"]),
            next_funding_time=(
                datetime.fromtimestamp(nft / 1000, tz=UTC) if nft else None
            ),
            raw=data,
        )

    # ── Extra helpers ───────────────────────────────────────────────

    def get_exchange_info(self) -> list[dict[str, Any]]:
        """Return raw symbol entries from ``/fapi/v1/exchangeInfo``."""
        resp = self._http.get("/fapi/v1/exchangeInfo")
        resp.raise_for_status()
        return resp.json().get("symbols", [])

    def get_perpetual_symbols(self) -> list[dict[str, str]]:
        """Return ``[{symbol, baseAsset, quoteAsset}, ...]`` for active perps."""
        return [
            {
                "symbol": s["symbol"],
                "baseAsset": s["baseAsset"],
                "quoteAsset": s["quoteAsset"],
            }
            for s in self.get_exchange_info()
            if s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ]

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BinanceData:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Internal ────────────────────────────────────────────────────

    @staticmethod
    def _parse_ticker(t: dict[str, Any]) -> Ticker:
        return Ticker(
            symbol=t["symbol"],
            last_price=Decimal(t["lastPrice"]),
            bid=Decimal(t["bidPrice"]) if t.get("bidPrice") else None,
            ask=Decimal(t["askPrice"]) if t.get("askPrice") else None,
            volume_24h=Decimal(t.get("quoteVolume", "0")),
            mark_price=Decimal(t["markPrice"]) if t.get("markPrice") else None,
            raw=t,
        )


# ════════════════════════════════════════════════════════════════════
# WebSocket — real-time public streams (no auth)
# ════════════════════════════════════════════════════════════════════


class BinanceWS:
    """Async WebSocket client for Binance Futures public streams.

    Usage::

        ws = BinanceWS()
        ws.on("btcusdt@ticker", handle_tick)
        await ws.connect(["btcusdt@ticker", "ethusdt@ticker"])
        await ws.listen()   # blocks until close()
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._log = get_logger("data_sources.binance.ws")
        self._ws_url = self._settings.binance_ws_url
        self._ws: Any = None
        self._callbacks: dict[str, list[Callable[[dict[str, Any]], None]]] = (
            defaultdict(list)
        )
        self._streams: list[str] = []
        self._running = False

    async def connect(self, streams: list[str]) -> None:
        """Connect to Binance combined stream.

        *streams*: ``["btcusdt@ticker", "ethusdt@kline_1d", ...]``
        """
        self._streams = streams
        combined = "/".join(streams)
        url = f"{self._ws_url}/stream?streams={combined}"
        self._ws = await websockets.connect(url, ping_interval=30, ping_timeout=10)
        self._log.info("ws_connected", stream_count=len(streams))

    def on(
        self, stream: str, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a callback for a specific stream name."""
        self._callbacks[stream].append(callback)

    async def listen(self) -> None:
        """Receive and dispatch messages.  Reconnects on disconnect."""
        self._running = True
        backoff = 1
        while self._running:
            try:
                async for raw in self._ws:
                    msg: dict[str, Any] = json.loads(raw)
                    stream = msg.get("stream", "")
                    data = msg.get("data", msg)
                    for cb in self._callbacks.get(stream, []):
                        try:
                            cb(data)
                        except Exception:
                            self._log.exception(
                                "ws_callback_error", stream=stream
                            )
                if not self._running:
                    break
            except ConnectionClosed:
                if not self._running:
                    break
                self._log.warning("ws_disconnected", backoff_s=backoff)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            try:
                await self.connect(self._streams)
                backoff = 1
            except Exception:
                self._log.exception("ws_reconnect_failed")

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
