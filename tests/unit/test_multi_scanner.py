"""Multiple scanner sets per bucket (Decision 026, Option A).

Each strategy row may name a scanner set via the optional ``scanner``
column; the set maps to scanner_<name>.yaml + allocator_<name>.yaml in
the bucket folder. Blank ⇒ the default scanner.yaml + allocator.yaml.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.shared.bucket import Bucket, BucketConfig, Market, TradingType
from src.shared.bucket_runner import BucketRunner
from src.shared.strategy_master.loader import (
    StrategyMasterError,
    load_strategy_master,
)
from src.shared.strategy_master.schema import StrategyMasterRow

_ROW = {
    "strategy_name": "s1",
    "tf": "1d",
    "min_vol": "",
    "trading_regime_1": "",
    "trading_regime_2": "",
    "trading_type": "longterm",
}


# ── schema ──────────────────────────────────────────────────────────────


def test_scanner_defaults_to_blank() -> None:
    row = StrategyMasterRow.model_validate(_ROW)
    assert row.scanner == ""


def test_scanner_none_cell_is_blank() -> None:
    # csv.DictReader yields None for a missing trailing cell.
    row = StrategyMasterRow.model_validate({**_ROW, "scanner": None})
    assert row.scanner == ""


def test_scanner_name_normalized_lowercase() -> None:
    row = StrategyMasterRow.model_validate({**_ROW, "scanner": " Momentum "})
    assert row.scanner == "momentum"


def test_scanner_bad_characters_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyMasterRow.model_validate({**_ROW, "scanner": "no-dashes!"})


# ── bucket path helpers ─────────────────────────────────────────────────


def _bucket(folder: Path) -> Bucket:
    return Bucket(
        id="longterm-crypto",
        trading_type=TradingType.LONGTERM,
        market=Market.CRYPTO,
        config=BucketConfig(
            capital_inr=Decimal("50000"),
            broker="delta_india",
            leverage_max=Decimal("5"),
        ),
        folder=folder,
    )


def test_default_scanner_paths_unchanged(tmp_path: Path) -> None:
    b = _bucket(tmp_path)
    assert b.scanner_yaml_path_for("") == tmp_path / "scanner.yaml"
    assert b.allocator_yaml_path_for("") == tmp_path / "allocator.yaml"


def test_named_scanner_paths(tmp_path: Path) -> None:
    b = _bucket(tmp_path)
    assert b.scanner_yaml_path_for("alt") == tmp_path / "scanner_alt.yaml"
    assert b.allocator_yaml_path_for("alt") == tmp_path / "allocator_alt.yaml"


# ── loader ──────────────────────────────────────────────────────────────

_HEADER_NO_SCANNER = (
    "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,trading_type\n"
)
_HEADER_SCANNER = (
    "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,"
    "trading_type,scanner\n"
)


def test_csv_without_scanner_column_defaults(tmp_path: Path) -> None:
    csv_path = tmp_path / "strategy_master.csv"
    csv_path.write_text(_HEADER_NO_SCANNER + "s1,1d,,,,longterm\n")
    master = load_strategy_master(csv_path, bucket_trading_type="longterm")
    assert master.by_name["s1"].scanner == ""


def test_csv_with_scanner_column(tmp_path: Path) -> None:
    csv_path = tmp_path / "strategy_master.csv"
    csv_path.write_text(
        _HEADER_SCANNER + "s1,1d,,,,longterm,\n" + "s2,1d,,,,longterm,alt\n"
    )
    master = load_strategy_master(csv_path, bucket_trading_type="longterm")
    assert master.by_name["s1"].scanner == ""
    assert master.by_name["s2"].scanner == "alt"


# ── runner config loading (fail-fast) ───────────────────────────────────

_SCANNER_YAML = (
    "universe_size: 5\n"
    "ranker:\n"
    "  name: volume_24h_desc\n"
    "  params: {}\n"
)
_ALLOCATOR_YAML = "fractional_kelly: 0.25\n"
_REGIME_YAML = (
    "enabled: false\n"
    "tf: 1d\n"
    "training_window_days: 30\n"
    "inference_lookback_bars: 50\n"
)


def _write_bucket_folder(folder: Path, *, with_alt_pair: bool) -> None:
    (folder / "strategies").mkdir(parents=True)
    (folder / "scanner.yaml").write_text(_SCANNER_YAML)
    (folder / "allocator.yaml").write_text(_ALLOCATOR_YAML)
    (folder / "regime.yaml").write_text(_REGIME_YAML)
    (folder / "strategy_master.csv").write_text(
        _HEADER_SCANNER + "s1,1d,,,,longterm,\n" + "s2,1d,,,,longterm,alt\n"
    )
    if with_alt_pair:
        (folder / "scanner_alt.yaml").write_text(_SCANNER_YAML)
        (folder / "allocator_alt.yaml").write_text(_ALLOCATOR_YAML)


def test_runner_loads_named_scanner_pair(tmp_path: Path) -> None:
    _write_bucket_folder(tmp_path, with_alt_pair=True)
    runner = BucketRunner(
        bucket=_bucket(tmp_path),
        brokers={},
        data=object(),  # type: ignore[arg-type]
        order_managers={},
    )
    assert set(runner.scanner_configs) == {"", "alt"}
    assert set(runner.allocator_configs) == {"", "alt"}
    # Default aliases still point at the default pair.
    assert runner.scanner_config is runner.scanner_configs[""]
    assert runner.allocator_config is runner.allocator_configs[""]


def test_runner_fails_fast_when_named_pair_missing(tmp_path: Path) -> None:
    _write_bucket_folder(tmp_path, with_alt_pair=False)
    with pytest.raises((FileNotFoundError, StrategyMasterError)):
        BucketRunner(
            bucket=_bucket(tmp_path),
            brokers={},
            data=object(),  # type: ignore[arg-type]
            order_managers={},
        )
