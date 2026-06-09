"""Unit tests for the EMA 9/15 crossover strategy.

Strategy under test:
    src/strategies/swing/crypto/strategies/ema_9_15.py

We test the entry logic in isolation by feeding synthetic OHLCV bars
through a fake MarketData implementation.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

import pytest

from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker


# ---------------------------------------------------------------------------
# Dynamic import — strategies live outside the importable namespace by design.
# ---------------------------------------------------------------------------
def _load_strategy_class():
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "strategies"
        / "swing"
        / "crypto"
        / "strategies"
        / "ema_9_15.py"
    )
    spec = importlib.util.spec_from_file_location("_ema_9_15_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Ema9_15Crossover


Ema9_15Crossover = _load_strategy_class()


# ---------------------------------------------------------------------------
# Fake MarketData
# ---------------------------------------------------------------------------
@dataclass
class FakeMarketData(MarketData):
    bars_by_symbol: dict[str, list[OHLCVBar]]

    def get_ohlcv(self, symbol: str, interval: str, limit: int = 500) -> list[OHLCVBar]:
        return self.bars_by_symbol.get(symbol, [])[-limit:]

    def get_ticker(self, symbol: str) -> Ticker:  # pragma: no cover - not used here
        return Ticker(symbol=symbol, last_price=Decimal("0"))

    def get_tickers(self) -> list[Ticker]:  # pragma: no cover
        return []

    def get_funding_rate(self, symbol: str) -> FundingRate:  # pragma: no cover
        return FundingRate(symbol=symbol, rate=Decimal("0"))


def _bars_from_closes(closes: Sequence[float]) -> list[OHLCVBar]:
    base = datetime(2026, 6, 10, tzinfo=timezone.utc)
    out: list[OHLCVBar] = []
    for i, c in enumerate(closes):
        out.append(
            OHLCVBar(
                timestamp=base + timedelta(hours=i),
                open=Decimal(str(c)),
                high=Decimal(str(c)),
                low=Decimal(str(c)),
                close=Decimal(str(c)),
                volume=Decimal("1000000"),
            )
        )
    return out


def _close_series_cross_up() -> list[float]:
    """30 bars: 29 flat at 100 so both EMAs converge there, then one big
    candle that lifts the fast EMA above the slow EMA on the LAST bar only.

    EMA(adjust=False) recursion: alpha_fast=0.2, alpha_slow=0.125. After
    a flat run both EMAs sit at 100, so the previous bar has fast == slow,
    and the spike on the last bar pushes fast above slow without retreating.
    """
    return [100.0] * 29 + [200.0]


def _close_series_flat() -> list[float]:
    """Flat noise — no crossover should fire."""
    return [100.0 + 0.0001 * ((-1) ** i) for i in range(30)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestEma9_15Crossover:
    def test_cross_up_emits_entry(self) -> None:
        strat = Ema9_15Crossover()
        bars = _bars_from_closes(_close_series_cross_up())
        data = FakeMarketData(bars_by_symbol={"BTCUSD": bars})

        entries = strat.select_entries(["BTCUSD"], data)
        assert len(entries) == 1
        e = entries[0]
        assert e.symbol == "BTCUSD"
        assert e.side == "buy"
        assert e.hint["signal"] == "ema_9_15_cross_up"
        assert e.hint["ema_fast"] > e.hint["ema_slow"]
        # gap_bps positive means fast > slow at the crossover bar
        assert e.hint["gap_bps"] > 0

    def test_flat_series_no_entry(self) -> None:
        strat = Ema9_15Crossover()
        bars = _bars_from_closes(_close_series_flat())
        data = FakeMarketData(bars_by_symbol={"FLATUSD": bars})

        entries = strat.select_entries(["FLATUSD"], data)
        assert entries == []

    def test_insufficient_bars_skipped(self) -> None:
        strat = Ema9_15Crossover()
        bars = _bars_from_closes([100.0] * 5)  # < min required
        data = FakeMarketData(bars_by_symbol={"NEWUSD": bars})

        entries = strat.select_entries(["NEWUSD"], data)
        assert entries == []

    def test_already_in_uptrend_no_new_signal(self) -> None:
        """If fast was already above slow on the previous bar, no entry."""
        # Pure uptrend — fast crossed slow long ago, not on the latest bar
        closes = [100.0 + i for i in range(30)]
        strat = Ema9_15Crossover()
        bars = _bars_from_closes(closes)
        data = FakeMarketData(bars_by_symbol={"BTCUSD": bars})

        entries = strat.select_entries(["BTCUSD"], data)
        assert entries == []

    def test_multiple_symbols_filters_to_crossers(self) -> None:
        strat = Ema9_15Crossover()
        data = FakeMarketData(
            bars_by_symbol={
                "BTCUSD": _bars_from_closes(_close_series_cross_up()),
                "FLATUSD": _bars_from_closes(_close_series_flat()),
            }
        )
        entries = strat.select_entries(["BTCUSD", "FLATUSD"], data)
        assert {e.symbol for e in entries} == {"BTCUSD"}

    def test_get_ohlcv_failure_is_skipped(self) -> None:
        class BoomData(FakeMarketData):
            def get_ohlcv(self, symbol, interval, limit=500):  # type: ignore[override]
                raise RuntimeError("network down")

        strat = Ema9_15Crossover()
        data = BoomData(bars_by_symbol={})
        # Should not raise — just skip the symbol.
        entries = strat.select_entries(["BTCUSD"], data)
        assert entries == []


# Quick sanity: pyt doesn't pick the file unless something runs.
def test_strategy_metadata() -> None:
    assert Ema9_15Crossover.name == "ema_9_15"
    assert Ema9_15Crossover.tf == "1h"
    assert Ema9_15Crossover.trading_type == "swing"
