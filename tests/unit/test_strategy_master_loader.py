"""CSV loader for strategy_master.csv."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from src.core.models import MarketRegime
from src.shared.strategy_master.loader import (
    StrategyMasterError,
    load_strategy_master,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "strategy_master.csv"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoader:
    def test_valid_rows(self, tmp_path: Path) -> None:
        csv = _write(
            tmp_path,
            "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,trading_type\n"
            "top5_volume,1d,1000000,bull,neutral,longterm\n"
            "mean_rev,1d,500000,neutral,,longterm\n",
        )
        m = load_strategy_master(csv, bucket_trading_type="longterm")
        assert len(m.rows) == 2
        assert "top5_volume" in m.by_name
        assert m.by_name["mean_rev"].min_vol == Decimal("500000")
        assert m.by_name["mean_rev"].trading_regime_1 == MarketRegime.NEUTRAL
        assert m.by_name["mean_rev"].trading_regime_2 is None

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(StrategyMasterError, match="missing"):
            load_strategy_master(
                tmp_path / "nope.csv", bucket_trading_type="longterm"
            )

    def test_missing_column_raises(self, tmp_path: Path) -> None:
        csv = _write(
            tmp_path, "strategy_name,tf,min_vol,trading_regime_1\nx,1d,100,bull\n"
        )
        with pytest.raises(StrategyMasterError, match="missing columns"):
            load_strategy_master(csv, bucket_trading_type="longterm")

    def test_type_mismatch_raises(self, tmp_path: Path) -> None:
        csv = _write(
            tmp_path,
            "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,trading_type\n"
            "x,1d,100,bull,,swing\n",
        )
        with pytest.raises(StrategyMasterError, match="trading_type"):
            load_strategy_master(csv, bucket_trading_type="longterm")

    def test_tf_mismatch_raises(self, tmp_path: Path) -> None:
        csv = _write(
            tmp_path,
            "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,trading_type\n"
            "x,1h,100,bull,,longterm\n",
        )
        with pytest.raises(StrategyMasterError, match="tf"):
            load_strategy_master(
                csv, bucket_trading_type="longterm", bucket_tf="1d"
            )

    def test_duplicate_name_raises(self, tmp_path: Path) -> None:
        csv = _write(
            tmp_path,
            "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,trading_type\n"
            "x,1d,100,bull,,longterm\n"
            "x,1d,200,neutral,,longterm\n",
        )
        with pytest.raises(StrategyMasterError, match="duplicate"):
            load_strategy_master(csv, bucket_trading_type="longterm")

    def test_blank_min_vol_is_none(self, tmp_path: Path) -> None:
        csv = _write(
            tmp_path,
            "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,trading_type\n"
            "x,1d,,bull,,longterm\n",
        )
        m = load_strategy_master(csv, bucket_trading_type="longterm")
        assert m.rows[0].min_vol is None

    def test_underscore_separated_min_vol(self, tmp_path: Path) -> None:
        csv = _write(
            tmp_path,
            "strategy_name,tf,min_vol,trading_regime_1,trading_regime_2,trading_type\n"
            "x,1d,1_000_000,bull,,longterm\n",
        )
        m = load_strategy_master(csv, bucket_trading_type="longterm")
        assert m.rows[0].min_vol == Decimal("1000000")
