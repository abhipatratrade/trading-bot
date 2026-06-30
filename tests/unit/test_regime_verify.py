"""Post-fit model verification (Markov 2.0 — FIX 2).

Imports ``hmmlearn`` (heavy). Skipped automatically if unavailable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("hmmlearn")

from src.shared.regime.diagnostics import persistence_diagnostic  # noqa: E402
from src.shared.regime.features import compute_features  # noqa: E402
from src.shared.regime.hmm_model import RegimeModel  # noqa: E402
from src.shared.regime.verify import verify_model  # noqa: E402


def _separated_bars(n: int = 1200, seed: int = 7) -> pd.DataFrame:
    """Cycling bear/neutral/bull segments — a model that *should* pass.

    Regimes alternate in short segments (not three long contiguous blocks),
    so the HMM sees frequent transitions, learns a well-conditioned
    transition matrix, and recovers balanced, return-ordered states. Return
    separation is large relative to within-segment noise so the model
    clusters on *return* (the labelling axis), not volatility.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="D")
    params = [(-0.025, 0.007), (0.000, 0.006), (0.025, 0.007)]  # bear, neutral, bull
    seg = 30
    returns: list[float] = []
    k = 0
    while len(returns) < n:
        mu, sd = params[k % 3]
        take = min(seg, n - len(returns))
        returns.extend(rng.normal(mu, sd, take))
        k += 1
    close = np.cumprod(1 + np.array(returns)) * 100
    volume = rng.uniform(1_000_000, 5_000_000, n)
    return pd.DataFrame({"close": close, "volume": volume}, index=idx)


def _fit(feature_columns=None, n_states=3, covariance_type="full"):
    cols = feature_columns or ("log_return", "realised_vol", "volume_zscore")
    feats = compute_features(_separated_bars(), feature_columns=cols)
    model = RegimeModel(
        feature_columns=list(feats.columns),
        n_states=n_states,
        covariance_type=covariance_type,
        n_restarts=4,
    ).fit(feats)
    return model, feats


class TestVerifyPasses:
    def test_healthy_three_state(self) -> None:
        model, feats = _fit()
        res = verify_model(model, feats)
        assert res.passed, res.reasons

    def test_healthy_two_state(self) -> None:
        model, feats = _fit(
            feature_columns=("log_return", "realised_vol"),
            n_states=2,
            covariance_type="diag",
        )
        res = verify_model(model, feats)
        assert res.passed, res.reasons


class TestVerifyRejects:
    def test_insufficient_separation(self) -> None:
        model, feats = _fit()
        res = verify_model(model, feats, min_mean_gap=1.0)  # impossibly large
        assert not res.passed
        assert any("separation" in r for r in res.reasons)

    def test_degenerate_state(self) -> None:
        model, feats = _fit()
        res = verify_model(model, feats, min_state_occupancy=0.9)  # 3 states can't each hold 90%
        assert not res.passed
        assert any("degenerate" in r for r in res.reasons)


class TestVerifyShape:
    def test_stats_and_as_dict(self) -> None:
        model, feats = _fit()
        res = verify_model(model, feats)
        assert "occupancy" in res.stats
        assert "fitted_mean_return" in res.stats
        assert "realised_mean_return" in res.stats
        assert set(res.as_dict()) == {"passed", "reasons", "warnings", "stats"}


class TestPersistenceDiagnostic:
    def test_runs_and_reports(self) -> None:
        model, feats = _fit()
        diag = persistence_diagnostic(model, feats, stride=14)
        d = diag.as_dict()
        assert "per_bar_pstay" in d
        assert "stride_pstay" in d
        assert isinstance(diag.inflated, bool)
        # Every labelled state appears in both views.
        assert set(diag.per_bar_pstay) == set(diag.stride_pstay)


class TestJsonSafe:
    """NaN/Inf must be scrubbed before stats reach Postgres JSONB."""

    def test_scrubs_non_finite(self) -> None:
        from src.shared.regime.retrain_job import _json_safe

        out = _json_safe(
            {
                "a": float("nan"),
                "b": [1.0, float("inf"), -float("inf")],
                "c": {"d": 0.5, "e": float("nan")},
                "s": "ok",
                "n": 3,
            }
        )
        assert out == {
            "a": None,
            "b": [1.0, None, None],
            "c": {"d": 0.5, "e": None},
            "s": "ok",
            "n": 3,
        }
