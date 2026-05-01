"""
Delta Exchange India public market data — REST only.

Used for 24h volume ranking (scanner), funding rates, and price data
from the execution venue.  Private data (positions, fills) lives in
``src.brokers.delta_india``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from src.core.config import Settings, get_settings
from src.core.logging import get_logger
from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker

# Seconds per candle interval — used to compute ``start`` timestamp from
# the requested ``limit`` (Delta requires explicit start/end).
_INTERVAL_SECS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


class DeltaIndiaData(MarketData):
    """Synchronous REST client for Delta Exchange India public data."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._log = get_logger("data_sources.delta_india")
        self._http = httpx.Client(
            base_url=self._settings.delta_base_url,
            timeout=10.0,
            headers={"User-Agent": "trading-bot/0.1.0"},
        )
        self._ticker_cache: dict[str, dict[str, Any]] | None = None

    # ── MarketData interface ────────────────────────────────────────

    def get_ohlcv(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[OHLCVBar]:
        secs = _INTERVAL_SECS.get(interval)
        if secs is None:
            raise ValueError(
                f"Unsupported interval {interval!r}; "
                f"valid: {list(_INTERVAL_SECS)}"
            )
        end = int(time.time())
        start = end - limit * secs
        resp = self._http.get(
            "/v2/history/candles",
            params={
                "resolution": interval,
                "symbol": symbol,
                "start": str(start),
                "end": str(end),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Delta candles error: {data.get('error')}")
        bars: list[OHLCVBar] = []
        for c in data["result"]:
            bars.append(
                OHLCVBar(
                    timestamp=datetime.fromtimestamp(c["time"], tz=UTC),
                    open=Decimal(str(c["open"])),
                    high=Decimal(str(c["high"])),
                    low=Decimal(str(c["low"])),
                    close=Decimal(str(c["close"])),
                    volume=Decimal(str(c["volume"])),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        return bars

    def get_ticker(self, symbol: str) -> Ticker:
        self._refresh_tickers()
        assert self._ticker_cache is not None
        raw = self._ticker_cache.get(symbol)
        if raw is None:
            raise ValueError(f"Unknown Delta symbol: {symbol}")
        return self._parse_ticker(symbol, raw)

    def get_tickers(self) -> list[Ticker]:
        self._refresh_tickers()
        assert self._ticker_cache is not None
        return [
            self._parse_ticker(sym, raw)
            for sym, raw in self._ticker_cache.items()
        ]

    def get_funding_rate(self, symbol: str) -> FundingRate:
        ticker = self.get_ticker(symbol)
        return FundingRate(
            symbol=symbol,
            rate=ticker.funding_rate or Decimal("0"),
            raw=ticker.raw,
        )

    # ── Extra helpers ───────────────────────────────────────────────

    def get_products(self) -> list[dict[str, Any]]:
        """Return raw product list from ``GET /v2/products``."""
        resp = self._http.get("/v2/products")
        resp.raise_for_status()
        return resp.json()["result"]

    def get_perpetual_symbols(self) -> list[dict[str, str]]:
        """Return ``[{symbol, baseAsset}, ...]`` for active perpetuals."""
        results: list[dict[str, str]] = []
        for p in self.get_products():
            if p.get("contract_type") != "perpetual_futures":
                continue
            underlying = p.get("underlying_asset", {})
            results.append(
                {
                    "symbol": p["symbol"],
                    "baseAsset": underlying.get("symbol", ""),
                }
            )
        return results

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DeltaIndiaData:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Internal ────────────────────────────────────────────────────

    def _refresh_tickers(self) -> None:
        resp = self._http.get("/v2/tickers")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Delta tickers error: {data.get('error')}")
        self._ticker_cache = {
            t["symbol"]: t for t in data["result"] if t.get("symbol")
        }

    @staticmethod
    def _parse_ticker(symbol: str, t: dict[str, Any]) -> Ticker:
        quotes = t.get("quotes", {})
        return Ticker(
            symbol=symbol,
            last_price=Decimal(str(t.get("close", 0))),
            bid=(
                Decimal(str(quotes["best_bid"]))
                if quotes.get("best_bid")
                else None
            ),
            ask=(
                Decimal(str(quotes["best_ask"]))
                if quotes.get("best_ask")
                else None
            ),
            volume_24h=Decimal(str(t.get("turnover_usd", 0))),
            open_interest=Decimal(str(t["oi_contracts"])) if t.get("oi_contracts") else None,
            mark_price=Decimal(str(t["mark_price"])) if t.get("mark_price") else None,
            funding_rate=Decimal(str(t["funding_rate"])) if t.get("funding_rate") else None,
            raw=t,
        )
