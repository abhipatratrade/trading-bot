"""buckets.yaml loader + folder layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.bucket import Market, TradingType, load_buckets


def test_loads_repo_root_buckets_yaml() -> None:
    """The actual repo buckets.yaml must load + match expected ids."""
    buckets = load_buckets()
    ids = {b.id for b in buckets}
    expected = {
        "longterm-crypto",
        "swing-crypto",
        "scalp-crypto",
        "gambling-crypto",
        "longterm-indian",
        "swing-indian",
    }
    assert expected.issubset(ids)


def test_longterm_crypto_resolves() -> None:
    buckets = {b.id: b for b in load_buckets()}
    b = buckets["longterm-crypto"]
    assert b.trading_type == TradingType.LONGTERM
    assert b.market == Market.CRYPTO
    # NOT asserting config.enabled: that is an operational switch the user
    # flips per rollout stage, not a structural property of the bucket.
    assert b.folder.is_dir()
    assert b.scanner_yaml_path.is_file()
    assert b.regime_yaml_path.is_file()
    assert b.allocator_yaml_path.is_file()
    assert b.strategy_master_csv_path.is_file()


def test_account_refs_isolated_per_crypto_bucket() -> None:
    """Decision 019: each crypto bucket maps to its own sub-account; the
    longterm bucket reuses the original 'default' account."""
    buckets = {b.id: b for b in load_buckets()}
    assert buckets["longterm-crypto"].config.account_ref == "default"
    crypto_refs = {
        buckets[bid].config.account_ref
        for bid in (
            "longterm-crypto",
            "swing-crypto",
            "scalp-crypto",
            "gambling-crypto",
        )
    }
    # Four crypto buckets → four distinct accounts (no shared netting).
    assert len(crypto_refs) == 4


def test_account_ref_defaults_when_absent(tmp_path: Path) -> None:
    yaml_path = tmp_path / "buckets.yaml"
    yaml_path.write_text(
        "buckets:\n"
        "  longterm-crypto:\n"
        "    capital_inr: 50000\n"
        "    broker: delta_india\n"
        "    leverage_max: 5\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    buckets = {
        b.id: b
        for b in load_buckets(
            buckets_yaml=yaml_path, strategies_root=tmp_path / "strategies"
        )
    }
    assert buckets["longterm-crypto"].config.account_ref == "default"


def test_missing_folder_raises(tmp_path: Path) -> None:
    yaml_path = tmp_path / "buckets.yaml"
    yaml_path.write_text(
        "buckets:\n"
        "  longterm-crypto:\n"
        "    capital_inr: 50000\n"
        "    broker: delta_india\n"
        "    leverage_max: 5\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError):
        load_buckets(buckets_yaml=yaml_path, strategies_root=tmp_path / "strategies")
