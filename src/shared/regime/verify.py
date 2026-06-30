"""
Post-fit model verification (Markov 2.0 — FIX 2, label verification).

A regime model that is confidently *mislabelled* is worse than no model:
the sizer/gate would act on a label that means the opposite of what it
says. ``label_states_by_mean_return`` already prevents the crude
bull/bear-swap bug by sorting on mean return, but two failure modes
survive that sort:

  - **Near-equal means.** With 3 states on noisy data two states can have
    almost identical mean returns, so which one is called NEUTRAL vs BULL
    is essentially a coin-flip that can flip between weekly retrains.
  - **Degenerate states.** A Baum-Welch local maximum can collapse a state
    to ~zero occupancy, leaving the model effectively 2-state but labelled
    as 3.

So before a freshly fitted model is allowed to ship, we run rule-based
checks against the *training data the model just saw* — no hard-coded
calendar dates, so it generalises across every coin:

  1. Monotonic separation — adjacent label tiers' mean log-returns differ
     by at least ``min_mean_gap``.
  2. Non-degenerate states — every state's Viterbi occupancy is at least
     ``min_state_occupancy``.
  3. Realised-return ordering — the bars each state actually covers are
     return-ordered the way their labels claim (BULL > NEUTRAL > BEAR).
  4. (warn-only) Volatility sanity — BEAR usually carries the highest
     realised vol.

A failing model is rejected by the retrain job, which keeps the previously
saved model rather than shipping a bad one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.core.models import MarketRegime
from src.shared.regime.hmm_model import RegimeModel

DEFAULT_MIN_MEAN_GAP: float = 0.0005      # ~5 bps/day separation in log-return
DEFAULT_MIN_STATE_OCCUPANCY: float = 0.03  # each state must cover >= 3% of bars


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of ``verify_model``. ``passed`` gates whether the model ships."""

    passed: bool
    reasons: list[str] = field(default_factory=list)   # fatal — why it failed
    warnings: list[str] = field(default_factory=list)  # non-fatal
    stats: dict[str, Any] = field(default_factory=dict)  # for extra JSONB/audit

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
        }


def verify_model(
    model: RegimeModel,
    features: pd.DataFrame,
    *,
    min_mean_gap: float = DEFAULT_MIN_MEAN_GAP,
    min_state_occupancy: float = DEFAULT_MIN_STATE_OCCUPANCY,
) -> VerifyResult:
    """Validate a fitted ``model`` against the ``features`` it was trained on.

    Returns a :class:`VerifyResult`; ``passed=False`` means the caller should
    reject the model and keep the previous one.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {}

    labels = model.labels                       # state index -> MarketRegime
    means = model.state_means()                 # (n_states, n_features)
    n_states = model.n_states
    ret_mean_by_state = means[:, 0]             # col 0 == log_return (convention)

    # Per-label fitted mean return (label -> mean), e.g. {BEAR: -0.004, ...}
    fitted_mean: dict[MarketRegime, float] = {
        labels[i]: float(ret_mean_by_state[i]) for i in range(n_states)
    }
    stats["fitted_mean_return"] = {k.value: v for k, v in fitted_mean.items()}

    # Ordering of labels we expect, low -> high mean return.
    expected_order = (
        [MarketRegime.BEAR, MarketRegime.NEUTRAL, MarketRegime.BULL]
        if n_states == 3
        else [MarketRegime.BEAR, MarketRegime.BULL]
    )

    # ---- 1) Monotonic separation -----------------------------------------
    ordered_means = [fitted_mean[r] for r in expected_order]
    gaps = [ordered_means[i + 1] - ordered_means[i] for i in range(len(ordered_means) - 1)]
    stats["mean_gaps"] = gaps
    for i, gap in enumerate(gaps):
        lo, hi = expected_order[i].value, expected_order[i + 1].value
        if gap < min_mean_gap:
            reasons.append(
                f"insufficient mean-return separation {lo}->{hi}: "
                f"gap={gap:.6f} < min={min_mean_gap:.6f}"
            )

    # ---- decode training data once (used by 2 & 3) -----------------------
    states = model.decode(features)             # raw int states per bar
    decoded_labels = np.array([labels[s].value for s in states])
    log_ret = features[model.feature_columns[0]].to_numpy(dtype=float)

    # ---- 2) Non-degenerate states ----------------------------------------
    occupancy: dict[str, float] = {}
    n_bars = len(states)
    for i in range(n_states):
        share = float(np.mean(states == i)) if n_bars else 0.0
        occupancy[labels[i].value] = share
        if share < min_state_occupancy:
            reasons.append(
                f"degenerate state {labels[i].value}: occupancy={share:.4f} "
                f"< min={min_state_occupancy:.4f}"
            )
    stats["occupancy"] = occupancy

    # ---- 3) Realised-return ordering (generalised bull/crash check) -------
    realised_mean: dict[str, float] = {}
    for r in expected_order:
        mask = decoded_labels == r.value
        realised_mean[r.value] = float(log_ret[mask].mean()) if mask.any() else float("nan")
    stats["realised_mean_return"] = realised_mean

    realised_seq = [realised_mean[r.value] for r in expected_order]
    if any(np.isnan(realised_seq)):
        reasons.append("a labelled state covers no decoded bars (cannot verify ordering)")
    else:
        for i in range(len(realised_seq) - 1):
            if not realised_seq[i + 1] > realised_seq[i]:
                lo, hi = expected_order[i].value, expected_order[i + 1].value
                reasons.append(
                    f"realised returns not ordered {lo}<{hi}: "
                    f"{realised_seq[i]:.6f} !< {realised_seq[i + 1]:.6f}"
                )

    # ---- 4) Volatility sanity (warn-only) --------------------------------
    if "realised_vol" in model.feature_columns:
        vol = features["realised_vol"].to_numpy(dtype=float)
        vol_by_label = {
            r.value: float(vol[decoded_labels == r.value].mean())
            if (decoded_labels == r.value).any()
            else float("nan")
            for r in expected_order
        }
        stats["realised_vol_by_label"] = vol_by_label
        bear_vol = vol_by_label.get(MarketRegime.BEAR.value)
        others = [
            v
            for k, v in vol_by_label.items()
            if k != MarketRegime.BEAR.value and not np.isnan(v)
        ]
        if bear_vol is not None and not np.isnan(bear_vol) and others:
            if bear_vol < max(others):
                warnings.append(
                    "BEAR is not the highest-volatility state "
                    f"(bear_vol={bear_vol:.6f}, max_other={max(others):.6f})"
                )

    return VerifyResult(
        passed=not reasons,
        reasons=reasons,
        warnings=warnings,
        stats=stats,
    )
