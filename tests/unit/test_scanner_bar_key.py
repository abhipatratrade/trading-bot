"""Scanner rows are keyed by BAR, not just by day.

Both scanner tables are written delete-then-insert. That is correct for a
once-a-day screen and destructive for swing-indian's meanrev cut, which runs on
every completed 1h bin: a day-scoped delete meant each of the 7 daily passes
erased the previous one, so only 15:16's rows ever survived a session. The
09:16 pass — the only one that reads the previous session's 15:15→15:30 stub,
and so the only path to the entry the backtest takes 3 times in 214 trades —
left no per-symbol trace at all.

These drive the real ``run_meanrev_scan`` against a capturing session, so they
fail if the bin scoping is ever dropped. Whether any symbol actually signals is
irrelevant here: persistence happens either way, which is exactly the property
under test.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete as sa_delete

from src.core.models import DailyUniverse, ScannerSnapshot
from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker
from src.shared.scanner import engine as eng
from src.shared.scanner.engine import RankerSpec, ScannerConfig

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
@dataclass
class _FakeData(MarketData):
    bars: dict[str, list[OHLCVBar]] = field(default_factory=dict)

    def get_ohlcv(self, symbol: str, interval: str, limit: int = 500) -> list[OHLCVBar]:
        return self.bars.get(symbol, [])[-limit:]

    def get_ticker(self, symbol: str) -> Ticker:  # pragma: no cover - unused
        return Ticker(symbol=symbol, last_price=Decimal("0"))

    def get_tickers(self) -> list[Ticker]:  # pragma: no cover - unused
        return []

    def get_funding_rate(self, symbol: str) -> FundingRate:  # pragma: no cover
        return FundingRate(symbol=symbol, rate=Decimal("0"))


class _CapturingSession:
    """Records what the scan would delete and insert, without a database."""

    def __init__(self) -> None:
        self.deletes: list[str] = []
        self.added: list[object] = []

    def execute(self, stmt):  # noqa: ANN001 - SQLAlchemy statement
        self.deletes.append(
            str(stmt.compile(compile_kwargs={"literal_binds": True}))
        )
        return None

    def add(self, obj: object) -> None:
        self.added.append(obj)


@pytest.fixture
def capture(monkeypatch) -> _CapturingSession:
    session = _CapturingSession()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(eng, "session_scope", _scope)
    # Reads the Trade ledger for the day's remaining entry budget — irrelevant
    # to persistence, and the only other DB touch in this path.
    monkeypatch.setattr(eng, "entries_taken_today", lambda *a, **k: 0)
    eng._MEANREV_SCAN_CACHE.clear()
    return session


def _bars(symbol: str) -> list[OHLCVBar]:
    """A handful of 15m bars — enough to resample, not enough to warm EMA20.

    Deliberate: every symbol comes back ``data_ema_warmup``, so the scan
    persists a full set of evaluated rows with no signals. Bar keying is
    orthogonal to whether anything crossed.
    """
    base = datetime(2026, 8, 3, 9, 15, tzinfo=IST).astimezone(UTC)
    return [
        OHLCVBar(
            timestamp=base + timedelta(minutes=15 * i),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
        )
        for i in range(24)
    ]


def _config(symbols: list[str]) -> ScannerConfig:
    return ScannerConfig(
        universe_size=5,
        filters=[],
        ranker=RankerSpec(name="volume_desc"),
        engine="equity_meanrev_1h",
        symbols=symbols,
    )


def _scan(capture: _CapturingSession, *, at: datetime) -> None:
    eng.run_meanrev_scan(
        bucket_id="swing-indian",
        data=_FakeData({s: _bars(s) for s in ("ACME", "WIDGET")}),
        config=_config(["ACME", "WIDGET"]),
        scan_date=at.date(),
        now=at.astimezone(UTC),
    )


# ---------------------------------------------------------------------------
# The schema itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model", [ScannerSnapshot, DailyUniverse])
def test_unique_key_includes_the_bar(model) -> None:
    """Without bar_key in the key, two bins of the same day cannot coexist."""
    unique = [
        c
        for c in model.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert unique, f"{model.__name__} lost its unique constraint"
    columns = {col.name for col in unique[0].columns}
    assert columns == {"date", "strategy_id", "symbol", "bar_key"}


@pytest.mark.parametrize("model", [ScannerSnapshot, DailyUniverse])
def test_bar_key_is_not_nullable(model) -> None:
    """Postgres treats NULLs as distinct in a UNIQUE constraint, so a nullable
    bar_key would silently disable the duplicate guard for daily scanners."""
    assert model.__table__.c.bar_key.nullable is False


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def test_meanrev_scan_stamps_the_scanned_bin(capture) -> None:
    _scan(capture, at=datetime(2026, 8, 3, 11, 16, tzinfo=IST))

    snapshots = [o for o in capture.added if isinstance(o, ScannerSnapshot)]
    assert len(snapshots) == 2  # one row per EVALUATED symbol
    # 11:16 scans the bin that closed at 11:15 — bin 1, not the one forming.
    assert {s.bar_key for s in snapshots} == {"2026-08-03#1"}


def test_meanrev_delete_is_scoped_to_the_bin_not_the_day(capture) -> None:
    """The regression guard. A day-scoped delete erased six of seven passes."""
    _scan(capture, at=datetime(2026, 8, 3, 12, 16, tzinfo=IST))

    assert len(capture.deletes) == 2  # snapshot + universe
    for sql in capture.deletes:
        assert "bar_key" in sql, f"delete is not bin-scoped: {sql}"
        assert "2026-08-03#2" in sql


def test_a_later_bin_does_not_erase_an_earlier_one(capture) -> None:
    """Two passes in one session target two different bins."""
    _scan(capture, at=datetime(2026, 8, 3, 11, 16, tzinfo=IST))
    first = list(capture.deletes)
    capture.deletes.clear()
    _scan(capture, at=datetime(2026, 8, 3, 12, 16, tzinfo=IST))

    assert all("2026-08-03#1" in sql for sql in first)
    assert all("2026-08-03#2" in sql for sql in capture.deletes)
    assert all("2026-08-03#1" not in sql for sql in capture.deletes)


def test_morning_pass_keys_rows_to_the_previous_sessions_stub(capture) -> None:
    """09:16 on a Monday scans Friday's 15:15→15:30 stub, and says so.

    This is the pass that previously left nothing behind: it ran first and was
    overwritten by every later bin of the day.
    """
    _scan(capture, at=datetime(2026, 8, 3, 9, 16, tzinfo=IST))

    snapshots = [o for o in capture.added if isinstance(o, ScannerSnapshot)]
    assert {s.bar_key for s in snapshots} == {"2026-07-31#6"}
    assert all("2026-07-31#6" in sql for sql in capture.deletes)


def test_rerunning_the_same_bin_still_replaces_cleanly(capture) -> None:
    """Idempotence — a restart mid-bin must not double-write the bin."""
    _scan(capture, at=datetime(2026, 8, 3, 12, 16, tzinfo=IST))
    eng._MEANREV_SCAN_CACHE.clear()  # a restart loses the in-process cache
    capture.deletes.clear()
    _scan(capture, at=datetime(2026, 8, 3, 12, 20, tzinfo=IST))

    # Same bin, so the second pass deletes exactly what the first wrote.
    assert all("2026-08-03#2" in sql for sql in capture.deletes)


def test_delete_targets_both_scanner_tables(capture) -> None:
    _scan(capture, at=datetime(2026, 8, 3, 11, 16, tzinfo=IST))
    tables = {
        "scanner_snapshot" if "scanner_snapshot" in sql else "daily_universe"
        for sql in capture.deletes
    }
    assert tables == {"scanner_snapshot", "daily_universe"}


def test_delete_statements_are_real_deletes(capture) -> None:
    """Guards the test doubles themselves: a captured SELECT would prove
    nothing about what the scan erases."""
    _scan(capture, at=datetime(2026, 8, 3, 11, 16, tzinfo=IST))
    assert all(sql.strip().upper().startswith("DELETE") for sql in capture.deletes)
    # sanity: the helper builds the same shape the scan does
    assert str(
        sa_delete(ScannerSnapshot).compile(compile_kwargs={"literal_binds": True})
    ).strip().upper().startswith("DELETE")
