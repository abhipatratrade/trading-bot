"""End-to-end: fit a small HMM and confirm it round-trips through to_dict / from_dict.

This test imports ``hmmlearn`` (heavy). Skipped automatically if unavailable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

hmmlearn = pytest.importorskip("hmmlearn")

from src.shared.regime.features import compute_features  # noqa: E402
from src.shared.regime.hmm_model import RegimeModel  # noqa: E402


def _synthetic_regime_bars(n: int = 600) -> pd.DataFrame:
    """Mixed bull/neutral/bear blocks to give the HMM something to find."""
    rng = np.random.default_rng(2026)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    block = n // 3
    returns = np.concatenate(
        [
            rng.normal(-0.005, 0.03, block),  # bear
            rng.normal(0.000, 0.01, block),   # neutral
            rng.normal(0.005, 0.02, n - 2 * block),  # bull
        ]
    )
    close = np.cumprod(1 + returns) * 100
    volume = rng.uniform(1_000_000, 5_000_000, n)
    return pd.DataFrame({"close": close, "volume": volume}, index=idx)


class TestHMMRoundTrip:
    def test_fit_predict_roundtrip(self) -> None:
        bars = _synthetic_regime_bars()
        feats = compute_features(bars)
        model = RegimeModel(feature_columns=list(feats.columns)).fit(feats)

        pred = model.predict_latest(feats, model_version="v_test")
        # Probabilities sum to 1
        assert abs(sum(pred.state_probabilities.values()) - 1.0) < 1e-6

        # Round-trip
        blob = model.to_dict()
        loaded = RegimeModel.from_dict(blob)
        pred2 = loaded.predict_latest(feats, model_version="v_test")
        assert pred.regime == pred2.regime
        for k, v in pred.state_probabilities.items():
            assert abs(pred2.state_probabilities[k] - v) < 1e-6
