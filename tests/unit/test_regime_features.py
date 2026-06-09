"""Feature pipeline + state-label mapping tests (no DB)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.core.models import MarketRegime
from src.shared.regime.features import compute_features
from src.shared.regime.regimes import label_states_by_mean_return


class TestCompute:
    def _make_bars(self, n: int = 200) -> pd.DataFrame:
        rng = np.random.default_rng(42)
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        close = np.cumprod(1 + rng.normal(0.001, 0.02, n)) * 100
        volume = rng.uniform(1_000_000, 5_000_000, n)
        return pd.DataFrame({"close": close, "volume": volume}, index=idx)

    def test_shape_after_warmup(self) -> None:
        bars = self._make_bars(200)
        feats = compute_features(bars)
        assert list(feats.columns) == [
            "log_return",
            "realised_vol",
            "volume_zscore",
        ]
        # vol_window=14 + volume_window=30 → ~30 warm-up rows lost
        assert len(feats) >= 170

    def test_missing_columns_raise(self) -> None:
        bars = pd.DataFrame({"close": [1, 2, 3]})
        with pytest.raises(ValueError, match="volume"):
            compute_features(bars)

    def test_empty_bars_raise(self) -> None:
        bars = pd.DataFrame({"close": [], "volume": []})
        with pytest.raises(ValueError):
            compute_features(bars)


class TestStateLabels:
    def test_three_state_monotone(self) -> None:
        # states ordered low → high return after mapping
        means = np.array(
            [
                [0.005, 0.02, 0.0],   # bull
                [-0.005, 0.04, 0.0],  # bear
                [0.001, 0.03, 0.0],   # neutral
            ]
        )
        labels = label_states_by_mean_return(means)
        assert labels[1] == MarketRegime.BEAR
        assert labels[2] == MarketRegime.NEUTRAL
        assert labels[0] == MarketRegime.BULL

    def test_non_three_state_raises(self) -> None:
        means = np.array([[0.0, 0.0], [0.001, 0.0]])
        with pytest.raises(ValueError, match="n_states must be 3"):
            label_states_by_mean_return(means)
