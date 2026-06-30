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

    @property
    def signal(self) -> float:
        """Continuous regime conviction in [-1, 1]: ``P(bull) − P(bear)``.

        Sign = direction, magnitude = conviction. Mirrors the Markov 2.0
        signal definition. The argmax ``regime`` label stays the thing the
        sizer/gate consume; this is an observability/forward-compat field.
        2-state models have no NEUTRAL key — the formula still holds (the
        missing term is simply 0).
        """
        p_bull = self.state_probabilities.get(MarketRegime.BULL, 0.0)
        p_bear = self.state_probabilities.get(MarketRegime.BEAR, 0.0)
        return p_bull - p_bear


class RegimeModel:
    """Fit + predict + serialise a Gaussian HMM.

    ``n_states`` and ``covariance_type`` are picked at construction by
    the retrain job's tiered selector — fewer parameters for symbols
    with less history. See ``shared/regime/retrain_job.py:pick_tier``.
    """

    random_state: int = 7
    n_iter: int = 200

    # Fixed, ordered seed pool for multi-restart fitting. The first entry is
    # ``random_state`` (7) so ``n_restarts=1`` reproduces the legacy single
    # fit exactly. Baum-Welch only finds *local* maxima — fitting several
    # seeds and keeping the best log-likelihood is the standard robustness
    # fix (the upstream skill notes this explicitly). The pool is fixed so
    # selection stays deterministic for a given ``n_restarts``.
    RESTART_SEEDS: tuple[int, ...] = (7, 13, 29, 101, 211, 379, 523, 661)

    def __init__(
        self,
        feature_columns: list[str],
        *,
        n_states: int = 3,
        covariance_type: str = "full",
        n_restarts: int = 1,
    ) -> None:
        if n_states not in (2, 3):
            raise ValueError(
                f"RegimeModel supports 2 or 3 states; got {n_states}"
            )
        if covariance_type not in ("full", "diag", "spherical", "tied"):
            raise ValueError(
                f"unsupported covariance_type: {covariance_type!r}"
            )
        if n_restarts < 1:
            raise ValueError(f"n_restarts must be >= 1; got {n_restarts}")
        self.feature_columns = list(feature_columns)
        self.n_states = n_states
        self.covariance_type = covariance_type
        self.n_restarts = n_restarts
        self._hmm: "GaussianHMM | None" = None
        self._labels: list[MarketRegime] | None = None
        # Diagnostics from the winning restart (runtime-only; not serialised).
        self.chosen_seed: int | None = None
        self.log_likelihood: float | None = None

    def _seeds(self) -> list[int]:
        """Deterministic seed list of length ``n_restarts``.

        Uses the fixed pool first; if more restarts are requested than the
        pool holds, extends it deterministically so selection stays
        reproducible.
        """
        pool = list(self.RESTART_SEEDS)
        if self.n_restarts <= len(pool):
            return pool[: self.n_restarts]
        extra = [pool[-1] + 131 * (i + 1) for i in range(self.n_restarts - len(pool))]
        return pool + extra

    # ------------------------------------------------------------------ fit
    def fit(self, features: pd.DataFrame) -> "RegimeModel":
        """Fit the HMM on a feature DataFrame and resolve state→label map.

        With ``n_restarts > 1`` the model is fit once per seed and the fit
        with the highest log-likelihood (``hmm.score``) is kept. Fits that
        raise are skipped; if every restart fails the last error is raised.
        """
        self._validate_columns(features)
        X = features[self.feature_columns].to_numpy(dtype=float)
        GaussianHMM = _import_gaussian_hmm()

        best_hmm: "GaussianHMM | None" = None
        best_score = float("-inf")
        best_seed: int | None = None
        last_err: Exception | None = None
        for seed in self._seeds():
            hmm = GaussianHMM(
                n_components=self.n_states,
                covariance_type=self.covariance_type,
                n_iter=self.n_iter,
                random_state=seed,
                init_params="stmc",
            )
            try:
                hmm.fit(X)
                score = float(hmm.score(X))
            except Exception as e:  # pragma: no cover — per-seed numerical
                last_err = e
                continue
            if not np.isfinite(score):
                continue
            if score > best_score:
                best_hmm, best_score, best_seed = hmm, score, seed

        if best_hmm is None:
            raise RuntimeError(
                "HMM fit failed for every restart seed"
            ) from last_err

        self._hmm = best_hmm
        self._labels = label_states_by_mean_return(best_hmm.means_)
        self.chosen_seed = best_seed
        self.log_likelihood = best_score
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

    # ------------------------------------------------------------- introspect
    @property
    def labels(self) -> list[MarketRegime]:
        """State-index → MarketRegime mapping (state ``i`` → ``labels[i]``)."""
        if self._labels is None:
            raise RuntimeError("RegimeModel is not fitted")
        return list(self._labels)

    def state_means(self) -> np.ndarray:
        """Per-state feature means, shape (n_states, n_features). Column 0 is
        ``log_return`` by project convention."""
        if self._hmm is None:
            raise RuntimeError("RegimeModel is not fitted")
        return np.asarray(self._hmm.means_, dtype=float)

    def transition_matrix(self) -> np.ndarray:
        """Fitted transition matrix (rows = from-state), shape (n, n)."""
        if self._hmm is None:
            raise RuntimeError("RegimeModel is not fitted")
        return np.asarray(self._hmm.transmat_, dtype=float)

    def decode(self, features: pd.DataFrame) -> np.ndarray:
        """Viterbi most-likely *raw state* path for ``features`` (ints 0..n-1)."""
        if self._hmm is None:
            raise RuntimeError("RegimeModel is not fitted")
        self._validate_columns(features)
        X = features[self.feature_columns].to_numpy(dtype=float)
        return np.asarray(self._hmm.predict(X), dtype=int)

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
        n_states = int(blob["n_states"])
        covariance_type = blob.get("covariance_type", "full")
        m = cls(
            feature_columns,
            n_states=n_states,
            covariance_type=covariance_type,
        )
        GaussianHMM = _import_gaussian_hmm()
        hmm = GaussianHMM(
            n_components=n_states,
            covariance_type=covariance_type,
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
        return m

    # ------------------------------------------------------------- internal
    def _validate_columns(self, features: pd.DataFrame) -> None:
        missing = set(self.feature_columns) - set(features.columns)
        if missing:
            raise ValueError(f"features missing columns: {sorted(missing)}")
