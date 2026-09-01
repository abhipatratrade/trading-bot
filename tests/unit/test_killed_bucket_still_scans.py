"""A halted bucket keeps SEEING — Phase 11a.

The August 2026 reconciliation read 11 signals across the two live Indian
buckets and found 4 filled. Chasing the blind days led here: the kill switch
used to ``return`` from ``BucketRunner.run_once`` before the scanner, so a
halted bucket wrote zero ``scanner_snapshot`` rows and the record could not
tell a halt from a quiet market. A ``stop_coverage`` trip over an unstopped
PIIND held swing-indian that way from 2026-08-12 to 08-18 — 28 scan bins, four
sessions, including the 08-17 Monday that took intraday-indian down with it.

Decision 024 says the switch blocks risk-INCREASING actions. Scanning increases
nothing. These tests pin both halves: the bucket still looks, and it still
enters nothing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.shared import bucket_runner as br
from src.shared.bucket import Bucket, BucketConfig, Market, TradingType
from src.shared.bucket_runner import BucketRunner
from src.shared.scanner.engine import ScanResult

_SCANNER_YAML = (
    "universe_size: 5\nranker:\n  name: volume_24h_desc\n  params: {}\n"
)
_ALLOCATOR_YAML = "fractional_kelly: 0.25\n"
_REGIME_YAML = (
    "enabled: false\ntf: 1d\ntraining_window_days: 30\n"
    "inference_lookback_bars: 50\n"
)
_HEADER = (
    "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,"
    "trading_type,scanner\n"
)

# Two strategies on two scanner sets — the shape that matters. The named set is
# reached ONLY from inside the per-strategy loop (`_scan_for(row.scanner)`), so
# a halt that skipped that loop wholesale would leave it unobserved. For
# intraday-indian the named set is `broad`, which is 100% of its live activity.
# One Strategy subclass per file — the loader enforces it.
_STRATEGY_PY = '''
from src.shared.base_strategy import Strategy, EntryCandidate


class {cls}(Strategy):
    name = "{name}"
    timeframe = "1d"

    def select_entries(self, candidates, data):
        return [EntryCandidate(symbol=s, side="buy") for s in candidates]
'''


class _StubBroker:
    """Only the surface run_once touches on the un-killed path."""

    def contract_size(self, symbol, default=None):
        return default


def _bucket(folder: Path) -> Bucket:
    return Bucket(
        id="swing-indian",
        trading_type=TradingType.SWING,
        market=Market.CRYPTO,  # 24/7 => always ENTRY_WINDOW, no calendar stub
        config=BucketConfig(
            capital_inr=Decimal("50000"),
            broker="delta_india",
            leverage_max=Decimal("5"),
        ),
        folder=folder,
    )


def _write_bucket(folder: Path) -> None:
    (folder / "strategies").mkdir(parents=True)
    (folder / "scanner.yaml").write_text(_SCANNER_YAML)
    (folder / "allocator.yaml").write_text(_ALLOCATOR_YAML)
    (folder / "regime.yaml").write_text(_REGIME_YAML)
    (folder / "scanner_broad.yaml").write_text(_SCANNER_YAML)
    (folder / "allocator_broad.yaml").write_text(_ALLOCATOR_YAML)
    (folder / "strategy_master.csv").write_text(
        _HEADER + "alpha,1d,,,,swing,\n" + "beta,1d,,,,swing,broad\n"
    )
    (folder / "strategies" / "alpha.py").write_text(
        _STRATEGY_PY.format(cls="Alpha", name="alpha")
    )
    (folder / "strategies" / "beta.py").write_text(
        _STRATEGY_PY.format(cls="Beta", name="beta")
    )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """A runner whose every side-effecting collaborator is recorded, not real."""
    _write_bucket(tmp_path)
    scanned: list[str] = []
    sized: list[str] = []

    def fake_run_scan(*, bucket_id, **kw):
        scanned.append(bucket_id)
        return ScanResult(
            bucket_id=bucket_id,
            date=date(2026, 8, 17),
            universe=["PIIND"],
            evaluated_count=94,
        )

    monkeypatch.setattr(br, "run_scan", fake_run_scan)
    monkeypatch.setattr(br, "predict_regime", lambda **kw: None)
    monkeypatch.setattr(
        br, "size_positions", lambda **kw: sized.append(kw) or {}
    )
    monkeypatch.setattr(BucketRunner, "_run_exits", lambda self, om: 0)
    monkeypatch.setattr(BucketRunner, "_roll_expiring", lambda self, om: None)
    # Only the UN-killed control gets this far. Stubbed so the control can
    # prove the sizer is reached without standing up a market-data double —
    # what it is guarding is the restructuring, not the pricing path.
    monkeypatch.setattr(
        BucketRunner,
        "_collect_mark_prices",
        lambda self, syms, failed=None: {s: Decimal("100") for s in syms},
    )

    runner = BucketRunner(
        bucket=_bucket(tmp_path),
        brokers={"default": _StubBroker()},  # type: ignore[dict-item]
        data=object(),  # type: ignore[arg-type]
        order_managers={"default": object()},  # type: ignore[dict-item]
    )
    return runner, scanned, sized


def _kill(monkeypatch, engaged: bool) -> None:
    monkeypatch.setattr(br.kill_switch, "is_engaged", lambda bid: engaged)


def test_killed_bucket_still_scans_every_set(harness, monkeypatch):
    """The regression itself: a halt must not stop the scan."""
    runner, scanned, _ = harness
    _kill(monkeypatch, True)

    runner.run_once()

    # Default set AND the named one. `swing-indian:broad` is namespaced by
    # run_scan's caller, which is how named snapshots avoid colliding.
    assert scanned == ["swing-indian", "swing-indian:broad"]


def test_killed_bucket_places_nothing(harness, monkeypatch):
    """The half that must not regress: still no risk-increasing action."""
    runner, _, sized = harness
    _kill(monkeypatch, True)

    summary = runner.run_once()

    assert sized == []  # never even reaches the sizer
    assert summary.placed == 0
    assert summary.eligible_strategies == []
    assert set(summary.blocked_strategies) == {"alpha", "beta"}
    assert all(
        v == "kill switch engaged" for v in summary.blocked_strategies.values()
    )


def test_killed_bucket_reports_the_universe_it_saw(harness, monkeypatch):
    """`universe=[]` was the old summary's lie — it had not looked, not found none."""
    runner, _, _ = harness
    _kill(monkeypatch, True)

    assert runner.run_once().universe == ["PIIND"]


def test_unkilled_bucket_reaches_the_sizer(harness, monkeypatch):
    """The control: the halt is what stops it, not the restructuring."""
    runner, scanned, sized = harness
    _kill(monkeypatch, False)

    summary = runner.run_once()

    assert scanned == ["swing-indian", "swing-indian:broad"]
    assert sorted(summary.eligible_strategies) == ["alpha", "beta"]
    assert summary.blocked_strategies == {}
    assert len(sized) == 2  # one sizing call per strategy
