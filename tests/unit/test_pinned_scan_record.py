"""The pinned engine leaves a trace of having looked — Phase 12b.

Until now ``engine: pinned`` persisted nothing: no scanner_snapshot, no
SCANNER_RUN. The September reconciliation counted zero rows for
commodity-indian 09-09 → 09-15 and reported it dead for a week the VM journal
shows it completing a pass every ~90 seconds. ``check_scan_coverage`` could not
see it either — ``coverage is None`` is its "no scan yet" answer.

These drive the real ``run_pinned_scan`` against a capturing session, the way
the meanrev bar-key tests do.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.core.models import AuditEventType, AuditLog, ScannerSnapshot
from src.shared.scanner import engine as eng
from src.shared.scanner.engine import RankerSpec, ScannerConfig, pinned_bar_key

IST = timezone(timedelta(hours=5, minutes=30))


class _CapturingSession:
    def __init__(self) -> None:
        self.deletes: list[str] = []
        self.added: list[object] = []

    def execute(self, stmt):  # noqa: ANN001
        self.deletes.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))

    def add(self, obj: object) -> None:
        self.added.append(obj)


@pytest.fixture
def capture(monkeypatch) -> _CapturingSession:
    session = _CapturingSession()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(eng, "session_scope", _scope)
    eng._PINNED_SCAN_CACHE.clear()
    return session


def _cfg(bar_minutes: int | None) -> ScannerConfig:
    return ScannerConfig(
        universe_size=1,
        ranker=RankerSpec(name="none", params={}),
        engine="pinned",
        symbols=["NATGASMINI"],
        bar_minutes=bar_minutes,
    )


def _ist(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 9, 12, int(h), int(m), 20, tzinfo=IST).astimezone(UTC)


# ── the key ─────────────────────────────────────────────────────────────


def test_bar_key_is_the_open_of_the_current_bin_in_ist():
    assert pinned_bar_key(_ist("13:07"), 15) == "2026-09-12#13:00"
    assert pinned_bar_key(_ist("13:15"), 15) == "2026-09-12#13:15"
    assert pinned_bar_key(_ist("23:29"), 15) == "2026-09-12#23:15"


def test_bar_key_handles_hourly_bins():
    assert pinned_bar_key(_ist("13:59"), 60) == "2026-09-12#13:00"


# ── the record ──────────────────────────────────────────────────────────


def test_one_run_writes_one_snapshot_and_one_audit_row(capture):
    result = eng.run_pinned_scan(
        bucket_id="commodity-indian",
        config=_cfg(15),
        scan_date=_ist("13:07").astimezone(IST).date(),
        now=_ist("13:07"),
    )
    assert result.universe == ["NATGASMINI"]
    snaps = [o for o in capture.added if isinstance(o, ScannerSnapshot)]
    audits = [o for o in capture.added if isinstance(o, AuditLog)]
    assert len(snaps) == 1 and snaps[0].bar_key == "2026-09-12#13:00"
    assert len(audits) == 1
    assert audits[0].event_type is AuditEventType.SCANNER_RUN
    assert audits[0].strategy_id == "commodity-indian"


def test_the_audit_payload_is_the_funnel_check_scan_coverage_reads(capture):
    """Same keys every other engine writes — the invariant parses them."""
    eng.run_pinned_scan(
        bucket_id="commodity-indian",
        config=_cfg(15),
        scan_date=_ist("13:07").astimezone(IST).date(),
        now=_ist("13:07"),
    )
    (audit,) = [o for o in capture.added if isinstance(o, AuditLog)]
    p = audit.payload
    assert p["configured"] == p["attempted"] == p["evaluated"] == 1
    assert p["unevaluable"] == 0
    assert p["bar_key"] == "2026-09-12#13:00"

    from src.safety.session_invariants import coverage_from_payload

    cov = coverage_from_payload(
        bucket_id="commodity-indian",
        scanner_id="commodity-indian",
        payload=p,
        ts=audit.ts or datetime.now(UTC),
    )
    assert cov is not None and cov.attempted == 1 and cov.evaluated == 1


def test_a_second_tick_in_the_same_bar_writes_nothing(capture):
    """~90s ticks, 15m bars: the record is per BAR, not per pass."""
    day = _ist("13:07").astimezone(IST).date()
    eng.run_pinned_scan(bucket_id="b", config=_cfg(15), scan_date=day, now=_ist("13:07"))
    n = len(capture.added)
    eng.run_pinned_scan(bucket_id="b", config=_cfg(15), scan_date=day, now=_ist("13:09"))
    assert len(capture.added) == n


def test_the_next_bar_writes_again(capture):
    day = _ist("13:07").astimezone(IST).date()
    eng.run_pinned_scan(bucket_id="b", config=_cfg(15), scan_date=day, now=_ist("13:07"))
    eng.run_pinned_scan(bucket_id="b", config=_cfg(15), scan_date=day, now=_ist("13:16"))
    keys = [o.bar_key for o in capture.added if isinstance(o, ScannerSnapshot)]
    assert keys == ["2026-09-12#13:00", "2026-09-12#13:15"]


def test_a_rerun_of_the_same_bar_deletes_before_inserting(capture):
    """Restart mid-bar: the delete is scoped to the BIN, like meanrev's."""
    day = _ist("13:07").astimezone(IST).date()
    eng.run_pinned_scan(bucket_id="b", config=_cfg(15), scan_date=day, now=_ist("13:07"))
    (d,) = capture.deletes
    assert "scanner_snapshot" in d and "2026-09-12#13:00" in d


def test_without_bar_minutes_nothing_is_written(capture):
    """The pre-12b contract, for any other pinned config."""
    result = eng.run_pinned_scan(
        bucket_id="b", config=_cfg(None), scan_date=_ist("13:07").date(), now=_ist("13:07")
    )
    assert result.universe == ["NATGASMINI"]
    assert capture.added == [] and capture.deletes == []


def test_commodity_indian_is_configured_for_it():
    from src.shared.bucket import load_bucket
    from src.shared.scanner.engine import load_scanner_config

    cfg = load_scanner_config(load_bucket("commodity-indian").scanner_yaml_path)
    assert cfg.engine == "pinned" and cfg.bar_minutes == 15
