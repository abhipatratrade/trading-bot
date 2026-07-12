"""Config-load smoke — every ENABLED bucket must boot (CI deploy gate, Layer 1).

``BucketRunner`` fail-fasts on bad config at construction (Decision 006), which
on the VM means a broken yaml/csv ships and then crashes the bot at startup.
This test front-loads exactly what the runner's constructor loads, so CI goes
red BEFORE the deploy gate lets the commit reach the VM.
"""

from __future__ import annotations

import pytest

from src.shared.allocator.sizer import load_allocator_config
from src.shared.bucket import load_buckets
from src.shared.regime.brain import load_regime_config
from src.shared.scanner.engine import load_scanner_config
from src.shared.strategy_loader import discover_strategies
from src.shared.strategy_master.loader import load_strategy_master

_ENABLED = [b for b in load_buckets() if b.config.enabled]


def test_at_least_one_bucket_enabled() -> None:
    assert _ENABLED, "no enabled buckets — buckets.yaml misconfigured?"


@pytest.mark.parametrize("bucket", _ENABLED, ids=lambda b: b.id)
def test_enabled_bucket_boots(bucket) -> None:
    """Load everything BucketRunner's constructor loads, fail-fast style."""
    master = load_strategy_master(
        bucket.strategy_master_csv_path,
        bucket_trading_type=bucket.trading_type.value,
    )
    assert master.rows, f"{bucket.id}: strategy_master.csv has no strategies"

    regime = load_regime_config(bucket.regime_yaml_path)
    assert regime.tf, f"{bucket.id}: regime tf missing"

    # Default pair + every named scanner set referenced by strategy rows
    # (Decision 026) — a named set with missing yaml fails the boot.
    for name in {""} | {row.scanner for row in master.rows}:
        load_scanner_config(bucket.scanner_yaml_path_for(name))
        load_allocator_config(bucket.allocator_yaml_path_for(name))

    # Importing strategy modules catches syntax/import errors in strategies/.
    strategies = discover_strategies(bucket.strategies_folder)
    assert strategies, f"{bucket.id}: no Strategy subclasses discovered"

    # Every strategy_master row must map to a discovered strategy class.
    missing = [r.strategy_name for r in master.rows
               if r.strategy_name not in strategies]
    assert not missing, f"{bucket.id}: rows without strategy code: {missing}"
