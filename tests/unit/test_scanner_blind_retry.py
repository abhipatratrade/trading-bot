"""A bin nobody could read is not a settled bin.

``run_meanrev_scan`` caches on the bar key alone, so one pass used to pin its
verdict for the whole bin. That is right for a bin that was actually read and
wrong for one that came back blind — and the 09:16 pass, the ONLY one that
ever reads the previous session's 15:15→15:30 stub, is precisely the pass most
likely to come back blind: it fires one minute after the open, and Dhan often
has not published the prior session's final 15m bar yet.

Live evidence (swing-indian, 10 sessions to 2026-08-26): the morning pass
reported ``data_bin_absent`` for the entire universe on 8 of them, and on
2026-08-07 — when the single attempt happened to land after Dhan published —
all 94 symbols resolved the stub normally. The bar is real, merely late. These
tests hold the retry that gives it a second look.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker
from src.shared.scanner import engine as eng
from src.shared.scanner.engine import RankerSpec, ScannerConfig
from src.shared.scanner.meanrev import MeanRevOutcome

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class _CountingData(MarketData):
    """Counts fetches, so a re-scan is visible even when the verdict is not."""

    fetches: int = field(default=0)

    def get_ohlcv(self, symbol: str, interval: str, limit: int = 500) -> list[OHLCVBar]:
        self.fetches += 1
        base = datetime(2026, 8, 25, 9, 15, tzinfo=IST).astimezone(UTC)
        return [
            OHLCVBar(
                timestamp=base + timedelta(minutes=15 * i),
                open=Decimal("100"), high=Decimal("101"),
                low=Decimal("99"), close=Decimal("100"),
                volume=Decimal("1000"),
            )
            for i in range(24)
        ]

    def get_ticker(self, symbol: str) -> Ticker:  # pragma: no cover - unused
        return Ticker(symbol=symbol, last_price=Decimal("0"))

    def get_tickers(self) -> list[Ticker]:  # pragma: no cover - unused
        return []

    def get_funding_rate(self, symbol: str) -> FundingRate:  # pragma: no cover
        return FundingRate(symbol=symbol, rate=Decimal("0"))


class _NullSession:
    def execute(self, stmt):  # noqa: ANN001 - SQLAlchemy statement
        return None

    def add(self, obj: object) -> None:
        return None


@pytest.fixture
def harness(monkeypatch):
    @contextmanager
    def _scope():
        yield _NullSession()

    monkeypatch.setattr(eng, "session_scope", _scope)
    monkeypatch.setattr(eng, "entries_taken_today", lambda *a, **k: 0)
    eng._MEANREV_SCAN_CACHE.clear()
    eng._MEANREV_BLIND_ATTEMPTS.clear()
    return monkeypatch


def _verdict(monkeypatch, reason: str) -> None:
    """Force every symbol to one outcome. ``data_*`` reasons are unevaluable."""
    monkeypatch.setattr(
        "src.shared.scanner.meanrev.evaluate_with_reason",
        lambda *a, **k: MeanRevOutcome(signal=None, reason=reason, metrics={}),
    )


def _scan(data: _CountingData, *, at: datetime) -> None:
    eng.run_meanrev_scan(
        bucket_id="swing-indian",
        data=data,
        config=ScannerConfig(
            universe_size=5,
            filters=[],
            ranker=RankerSpec(name="volume_desc"),
            engine="equity_meanrev_1h",
            symbols=["ACME", "WIDGET"],
        ),
        scan_date=at.date(),
        now=at.astimezone(UTC),
    )


# The 09:16 pass: bin key is the PREVIOUS session's stub.
_MORNING = datetime(2026, 8, 26, 9, 16, tzinfo=IST)


def test_a_blind_bin_is_refetched_after_the_gap(harness) -> None:
    """The stub bin gets more than the one attempt Dhan usually loses."""
    _verdict(harness, "data_bin_absent")
    data = _CountingData()

    _scan(data, at=_MORNING)
    first = data.fetches
    assert first > 0

    # Same bin, one tick later: too soon, still served from cache.
    _scan(data, at=_MORNING + timedelta(minutes=1))
    assert data.fetches == first, "retried before the gap elapsed"

    # Past the gap, inside the same bin: looked at again.
    _scan(data, at=_MORNING + timedelta(minutes=6))
    assert data.fetches > first, "a blind bin was never re-fetched"


def test_a_bin_that_was_read_is_not_refetched(harness) -> None:
    """The cache still does its job — this is the ~190-calls/min guard."""
    _verdict(harness, "no_fresh_cross")
    data = _CountingData()

    _scan(data, at=_MORNING)
    settled = data.fetches

    _scan(data, at=_MORNING + timedelta(minutes=6))
    _scan(data, at=_MORNING + timedelta(minutes=30))
    assert data.fetches == settled, "a bin that WAS read got re-fetched"


def test_retries_are_bounded(harness) -> None:
    """A genuinely dead morning must not re-fetch every tick until 10:16."""
    _verdict(harness, "data_bin_absent")
    data = _CountingData()

    # Ten tries spread over 09:16→10:10 — all inside the stub bin, which only
    # rolls when 10:15 passes.
    at = _MORNING
    for _ in range(10):
        _scan(data, at=at)
        at += timedelta(minutes=6)

    per_scan = 2 * 2  # 2 symbols × (intraday + daily)
    assert data.fetches <= per_scan * eng._BLIND_RETRY_MAX, "retries unbounded"


def test_a_new_bin_gets_its_own_budget(harness) -> None:
    """Exhausting the stub bin must not blind the 10:16 pass that follows."""
    _verdict(harness, "data_bin_absent")
    data = _CountingData()

    at = _MORNING
    for _ in range(10):  # burn the stub bin's retries
        _scan(data, at=at)
        at += timedelta(minutes=6)
    spent = data.fetches

    _scan(data, at=datetime(2026, 8, 26, 10, 16, tzinfo=IST))
    assert data.fetches > spent, "the next bin inherited an exhausted budget"


def test_recovery_clears_the_retry_state(harness) -> None:
    """Once the bar lands, the bin settles and stops costing fetches."""
    _verdict(harness, "data_bin_absent")
    data = _CountingData()
    _scan(data, at=_MORNING)

    _verdict(harness, "no_fresh_cross")  # Dhan published the stub
    _scan(data, at=_MORNING + timedelta(minutes=6))
    recovered = data.fetches

    _scan(data, at=_MORNING + timedelta(minutes=12))
    assert data.fetches == recovered, "kept retrying a bin that resolved"
    assert "swing-indian" not in eng._MEANREV_BLIND_ATTEMPTS
