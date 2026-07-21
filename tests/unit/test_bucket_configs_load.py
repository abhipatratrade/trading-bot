"""Config-load smoke — every CONFIGURED bucket must boot (CI deploy gate, Layer 1).

``BucketRunner`` fail-fasts on bad config at construction (Decision 006), which
on the VM means a broken yaml/csv ships and then crashes the bot at startup.
This test front-loads exactly what the runner's constructor loads, so CI goes
red BEFORE the deploy gate lets the commit reach the VM.

Scope is every bucket with a populated folder, NOT just the enabled ones.
Parametrising over the enabled set made the check silently VACUOUS whenever
buckets were paused — which is exactly when a staged rollout is editing config
and most needs the validation. A bucket about to be switched on must already
have been proven to load.
"""

from __future__ import annotations

import pytest

from src.shared.allocator.sizer import load_allocator_config
from src.shared.bucket import load_buckets
from src.shared.regime.brain import load_regime_config
from src.shared.scanner.engine import load_scanner_config
from src.shared.strategy_loader import discover_strategies
from src.shared.strategy_master.loader import load_strategy_master


def _declares_strategies(bucket) -> bool:
    """True when the bucket's strategy_master.csv has at least one data row.

    Phase 5/6 stubs (scalp-crypto, gambling-crypto, longterm-indian) ship a
    header-only CSV as a placeholder — they are not yet implemented and have
    nothing to validate.
    """
    path = bucket.strategy_master_csv_path
    if not path.is_file():
        return False
    lines = [
        ln for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()
    ]
    return len(lines) > 1


# Buckets that actually declare strategies. The enabled flag is deliberately
# NOT part of the filter — see the module docstring.
_CONFIGURED = [b for b in load_buckets() if _declares_strategies(b)]


def test_buckets_yaml_parses_and_has_configured_buckets() -> None:
    assert _CONFIGURED, "no bucket has a strategy_master.csv — misconfigured?"


def test_enabled_set_is_a_subset_of_configured() -> None:
    """Anything switched on must have a config set this file actually checks.

    An enabled bucket with no strategy_master.csv would boot straight into a
    fail-fast on the VM. The enabled set may legitimately be EMPTY during a
    staged rollout, so emptiness itself is not an error.
    """
    enabled = {b.id for b in load_buckets() if b.config.enabled}
    configured = {b.id for b in _CONFIGURED}
    assert enabled <= configured, f"enabled but unconfigured: {enabled - configured}"


@pytest.mark.parametrize("bucket", _CONFIGURED, ids=lambda b: b.id)
def test_configured_bucket_boots(bucket) -> None:
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
