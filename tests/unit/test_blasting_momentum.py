"""Blasting Momentum strategy — entries + Supertrend/max-hold exits (Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.data_sources.base import OHLCVBar
from src.strategies.swing.indian.strategies.blasting_momentum import (
    BlastingMomentum,
)


class _FakeData:
    def __init__(self, bars: list[OHLCVBar]) -> None:
        self._bars = bars

    def get_ohlcv(self, symbol: str, interval: str, limit: int = 500):  # noqa: ARG002
        return self._bars


class _Pos:
    """Minimal stand-in for a Position row (strategy only reads opened_at)."""

    def __init__(self, opened_at: datetime) -> None:
        self.opened_at = opened_at


def _daily(closes: list[float]) -> list[OHLCVBar]:
    return [
        OHLCVBar(
            timestamp=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=i),
            open=Decimal(str(c)), high=Decimal(str(c + 0.5)),
            low=Decimal(str(c - 0.5)), close=Decimal(str(c)), volume=Decimal("1"),
        )
        for i, c in enumerate(closes)
    ]


_RISING = [100.0 + i for i in range(40)]           # steady uptrend
_CRASH = [100.0 + i for i in range(35)] + [110, 95, 80, 70]  # blow-off then crash


def test_select_entries_claims_all_candidates() -> None:
    strat = BlastingMomentum()
    cands = strat.select_entries(["SWIGGY", "TBZ"], _FakeData([]))
    assert [c.symbol for c in cands] == ["SWIGGY", "TBZ"]
    assert all(c.side == "buy" for c in cands)


def test_exit_on_supertrend_flip() -> None:
    strat = BlastingMomentum()
    held = {"TBZ": _Pos(datetime.now(UTC) - timedelta(days=3))}  # young position
    exits = strat.select_exits(held, _FakeData(_daily(_CRASH)))
    assert exits == ["TBZ"]


def test_no_exit_while_trending_up() -> None:
    strat = BlastingMomentum()
    held = {"SWIGGY": _Pos(datetime.now(UTC) - timedelta(days=3))}
    exits = strat.select_exits(held, _FakeData(_daily(_RISING)))
    assert exits == []


def test_exit_on_max_hold_days() -> None:
    strat = BlastingMomentum()
    # 31 days old → past the 30-day cap, exits regardless of price action.
    held = {"SWIGGY": _Pos(datetime.now(UTC) - timedelta(days=31))}
    exits = strat.select_exits(held, _FakeData(_daily(_RISING)))
    assert exits == ["SWIGGY"]
