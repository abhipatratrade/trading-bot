"""
HMM wrapper.

A thin shell around ``hmmlearn.GaussianHMM`` that handles:
- fitting on a feature DataFrame,
- mapping internal states to MarketRegime labels (sorted by mean return),
- predicting the most-recent regime + state probabilities,
- serialising the fitted parameters to a JSONB-friendly dict.

We deliberately don't expose hmmlearn's full API. Strategies and the
runner only need ``fit``, ``predict_latest``, and ``to_dict`` /
``from_dict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from src.core.models import MarketRegime
from src.shared.regime.regimes import label_states_by_mean_return

# hmmlearn is heavy (scipy + native build) and is only needed when actually
# fitting / loading a model. Importing it lazily lets modules that *transit*
# through this file (e.g. the scheduler service registering retrain jobs,
# the dashboard rendering bucket pages) start up cleanly on environments
# where hmmlearn isn't installed — they only break if they actually try
# to run regime code. See Railway scheduler crash 2026-06-12.
if TYPE_CHECKING:
    from hmmlearn.hmm import GaussianHMM


def _import_gaussian_hmm() -> type["GaussianHMM"]:
    """Import hmmlearn on first use. Raises ImportError with a clear message
    if the dependency is missing in this environment."""
    try:
        from hmmlearn.hmm import GaussianHMM as _GaussianHMM
    except ImportError as e:  # pragma: no cover  — environmental
        raise ImportError(
            "hmmlearn is not installed. Run `pip install hmmlearn scipy` "
            "(needed only by the regime-retrain job, not the dashboard / "
            "scheduler boot path)."
        ) from e
    return _GaussianHMM


@dataclass(frozen=True, slots=True)
class RegimePrediction:
    regime: MarketRegime
    state_probabilities: dict[MarketRegime, float]
    model_version: str


class RegimeModel:
    """Fit + predict + serialise a Gaussian HMM with 3 hidden states."""

    n_states: int = 3
    covariance_type: str = "full"
    random_state: int = 7
    n_iter: int = 200

    def __init__(self, feature_columns: list[str]) -> None:
        self.feature_columns = list(feature_columns)
        self._hmm: "GaussianHMM | None" = None
        self._labels: list[MarketRegime] | None = None

    # ------------------------------------------------------------------ fit
    def fit(self, features: pd.DataFrame) -> "RegimeModel":
        """Fit the HMM on a feature DataFrame and resolve state→label map."""
        self._validate_columns(features)
        X = features[self.feature_columns].to_numpy(dtype=float)
        GaussianHMM = _import_gaussian_hmm()
        hmm = GaussianHMM(
            n_components=self.n_states,
            covariance_type=self.covariance_type,
            n_iter=self.n_iter,
            random_state=self.random_state,
            init_params="stmc",
        )
        hmm.fit(X)
        self._hmm = hmm
        self._labels = label_states_by_mean_return(hmm.means_)
        return self

    # -------------------------------------------------------------- predict
    def predict_latest(
        self, features: pd.DataFrame, model_version: str
    ) -> RegimePrediction:
        """Return the most recent regime + state-probability vector.

        Uses smoothed (posterior) probabilities from ``predict_proba``,
        not the raw Viterbi path — smoothing reduces single-bar flicker.
        """
        if self._hmm is None or self._labels is None:
            raise RuntimeError("RegimeModel is not fitted")
        self._validate_columns(features)
        X = features[self.feature_columns].to_numpy(dtype=float)
        if X.shape[0] == 0:
            raise ValueError("features has no rows to predict")
        probs = self._hmm.predict_proba(X)
        latest = probs[-1]
        state_probs = {
            self._labels[i]: float(latest[i]) for i in range(self.n_states)
        }
        regime = max(state_probs.items(), key=lambda kv: kv[1])[0]
        return RegimePrediction(
            regime=regime,
            state_probabilities=state_probs,
            model_version=model_version,
        )

    # ------------------------------------------------------------ serialise
    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSONB-friendly dict. Round-trip via ``from_dict``."""
        if self._hmm is None or self._labels is None:
            raise RuntimeError("RegimeModel is not fitted")
        return {
            "feature_columns": self.feature_columns,
            "n_states": self.n_states,
            "covariance_type": self.covariance_type,
            "labels": [lab.value for lab in self._labels],
            "startprob": self._hmm.startprob_.tolist(),
            "transmat": self._hmm.transmat_.tolist(),
            "means": self._hmm.means_.tolist(),
            "covars": self._hmm.covars_.tolist(),
        }

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> "RegimeModel":
        """Rebuild from a ``to_dict`` payload."""
        feature_columns = list(blob["feature_columns"])
        m = cls(feature_columns)
        n_states = int(blob["n_states"])
        GaussianHMM = _import_gaussian_hmm()
        hmm = GaussianHMM(
            n_components=n_states,
            covariance_type=blob.get("covariance_type", cls.covariance_type),
            n_iter=cls.n_iter,
            random_state=cls.random_state,
            init_params="",  # we set the params manually below
        )
        hmm.startprob_ = np.asarray(blob["startprob"], dtype=float)
        hmm.transmat_ = np.asarray(blob["transmat"], dtype=float)
        hmm.means_ = np.asarray(blob["means"], dtype=float)
        hmm.covars_ = np.asarray(blob["covars"], dtype=float)
        # GaussianHMM requires n_features to be set for predict_proba.
        hmm.n_features = hmm.means_.shape[1]
        m._hmm = hmm
        m._labels = [MarketRegime(v) for v in blob["labels"]]
        m.n_states = n_states
        return m

    # ------------------------------------------------------------- internal
    def _validate_columns(self, features: pd.DataFrame) -> None:
        missing = set(self.feature_columns) - set(features.columns)
        if missing:
            raise ValueError(f"features missing columns: {sorted(missing)}")
