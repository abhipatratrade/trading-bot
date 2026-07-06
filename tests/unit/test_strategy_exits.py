"""Unit tests for strategy exit logic (Decision 021).

Covers:
    - top5_volume: exit when the symbol's regime flips to BEAR
    - ema_9_15: exit when EMA(9) sits below EMA(15)
    - dashboard basic-auth header check (pure function)
"""

from __future__ import annotations

import base64
import importlib.util
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from src.core.models import MarketRegime
from src.dashboard.app import _check_basic_auth
from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker

# ---------------------------------------------------------------------------
# Dynamic strategy imports (strategies live outside the importable namespace)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, cls_name: str):
    spec = importlib.util.spec_from_file_location(f"_exit_test_{cls_name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, cls_name)


Top5VolumeLongterm = _load(
    _ROOT / "src" / "strategies" / "longterm" / "crypto" / "strategies" / "top5_volume.py",
    "Top5VolumeLongterm",
)
Ema9_15Crossover = _load(
    _ROOT / "src" / "strategies" / "swing" / "crypto" / "strategies" / "ema_9_15.py",
    "Ema9_15Crossover",
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
@dataclass
class FakeMarketData(MarketData):
    bars_by_symbol: dict[str, list[OHLCVBar]]

    def get_ohlcv(self, symbol: str, interval: str, limit: int = 500) -> list[OHLCVBar]:
        return self.bars_by_symbol.get(symbol, [])[-limit:]

    def get_ticker(self, symbol: str) -> Ticker:  # pragma: no cover
        return Ticker(symbol=symbol, last_price=Decimal("0"))

    def get_tickers(self) -> list[Ticker]:  # pragma: no cover
        return []

    def get_funding_rate(self, symbol: str) -> FundingRate:  # pragma: no cover
        return FundingRate(symbol=symbol, rate=Decimal("0"))


class _FakePosition:
    """Stands in for the ORM Position — exit logic only reads the mapping keys."""


def _bars_from_closes(closes: Sequence[float]) -> list[OHLCVBar]:
    base = datetime(2026, 6, 10, tzinfo=UTC)
    return [
        OHLCVBar(
            timestamp=base + timedelta(hours=i),
            open=Decimal(str(c)),
            high=Decimal(str(c)),
            low=Decimal(str(c)),
            close=Decimal(str(c)),
            volume=Decimal("1000000"),
        )
        for i, c in enumerate(closes)
    ]


# ---------------------------------------------------------------------------
# top5_volume — regime-flip exits
# ---------------------------------------------------------------------------
class TestTop5VolumeExits:
    def test_bear_regime_exits(self) -> None:
        strat = Top5VolumeLongterm()
        held = {"BTCUSD": _FakePosition(), "ETHUSD": _FakePosition()}
        regimes = {
            "BTCUSD": MarketRegime.BEAR,
            "ETHUSD": MarketRegime.BULL,
        }
        exits = strat.select_exits(held, FakeMarketData({}), regimes)
        assert exits == ["BTCUSD"]

    def test_neutral_and_bull_hold(self) -> None:
        strat = Top5VolumeLongterm()
        held = {"BTCUSD": _FakePosition(), "ETHUSD": _FakePosition()}
        regimes = {
            "BTCUSD": MarketRegime.NEUTRAL,
            "ETHUSD": MarketRegime.BULL,
        }
        assert strat.select_exits(held, FakeMarketData({}), regimes) == []

    def test_missing_regime_holds(self) -> None:
        strat = Top5VolumeLongterm()
        held = {"BTCUSD": _FakePosition()}
        assert strat.select_exits(held, FakeMarketData({}), {"BTCUSD": None}) == []

    def test_no_regimes_at_all_holds(self) -> None:
        strat = Top5VolumeLongterm()
        held = {"BTCUSD": _FakePosition()}
        assert strat.select_exits(held, FakeMarketData({}), None) == []


# ---------------------------------------------------------------------------
# ema_9_15 — EMA-state exits
# ---------------------------------------------------------------------------
class TestEmaExits:
    def test_downtrend_exits(self) -> None:
        strat = Ema9_15Crossover()
        # Long downtrend → fast EMA well below slow EMA.
        bars = _bars_from_closes([200.0 - 2 * i for i in range(30)])
        data = FakeMarketData(bars_by_symbol={"BTCUSD": bars})
        exits = strat.select_exits({"BTCUSD": _FakePosition()}, data)
        assert exits == ["BTCUSD"]

    def test_uptrend_holds(self) -> None:
        strat = Ema9_15Crossover()
        bars = _bars_from_closes([100.0 + i for i in range(30)])
        data = FakeMarketData(bars_by_symbol={"BTCUSD": bars})
        assert strat.select_exits({"BTCUSD": _FakePosition()}, data) == []

    def test_fetch_failure_holds(self) -> None:
        class BoomData(FakeMarketData):
            def get_ohlcv(self, symbol, interval, limit=500):  # type: ignore[override]
                raise RuntimeError("network down")

        strat = Ema9_15Crossover()
        assert strat.select_exits({"BTCUSD": _FakePosition()}, BoomData({})) == []

    def test_insufficient_bars_holds(self) -> None:
        strat = Ema9_15Crossover()
        data = FakeMarketData(bars_by_symbol={"BTCUSD": _bars_from_closes([100.0] * 5)})
        assert strat.select_exits({"BTCUSD": _FakePosition()}, data) == []


# ---------------------------------------------------------------------------
# Dashboard basic auth header check
# ---------------------------------------------------------------------------
class TestBasicAuth:
    @staticmethod
    def _header(user: str, password: str) -> str:
        raw = base64.b64encode(f"{user}:{password}".encode()).decode()
        return f"Basic {raw}"

    def test_correct_credentials_pass(self) -> None:
        assert _check_basic_auth(self._header("admin", "s3cret"), "admin", "s3cret")

    def test_wrong_password_fails(self) -> None:
        assert not _check_basic_auth(self._header("admin", "nope"), "admin", "s3cret")

    def test_wrong_user_fails(self) -> None:
        assert not _check_basic_auth(self._header("bob", "s3cret"), "admin", "s3cret")

    def test_missing_header_fails(self) -> None:
        assert not _check_basic_auth(None, "admin", "s3cret")

    def test_garbage_header_fails(self) -> None:
        assert not _check_basic_auth("Basic !!!not-base64!!!", "admin", "s3cret")
        assert not _check_basic_auth("Bearer whatever", "admin", "s3cret")
