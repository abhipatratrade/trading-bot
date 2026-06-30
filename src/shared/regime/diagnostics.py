"""
Persistence / autocorrelation diagnostic (Markov 2.0 — FIX 1, adapted).

The upstream skill's FIX 1 warns that building a transition matrix from
*overlapping* rolling windows fakes stickiness on the diagonal, and tells
you to always compute both the overlapping (legacy) and the stride-sampled
(honest) matrix and show them side by side.

We don't build a counted matrix — we fit a Gaussian HMM. But two of our
three features (``realised_vol``, ``volume_zscore``) *are* overlapping
rolling windows, so consecutive bars' feature vectors are strongly
autocorrelated. That violates the HMM's emission-independence assumption
and biases the estimated transition matrix toward over-persistence — the
same disease through a different door.

So we run the same side-by-side comparison on the **decoded state path**:

  - per-bar      — empirical P(stay) counted between every consecutive bar
  - stride-sampled — empirical P(stay) counted between bars ``stride`` apart

If the per-bar diagonal is materially higher than the stride-sampled one,
the model's persistence is partly an autocorrelation artefact. This is
**reporting only** — it changes nothing about the model or trading; it
lands in the model row's ``extra`` JSONB and the logs (House Rule 8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.shared.regime.hmm_model import RegimeModel

DEFAULT_STRIDE: int = 14            # ~ realised_vol window
INFLATION_WARN_THRESHOLD: float = 0.10  # mean diagonal gap that triggers a warning


def _diag_pstay(seq: np.ndarray, n_states: int) -> dict[int, float]:
    """Per-state empirical P(stay) from a state sequence."""
    counts = np.zeros((n_states, n_states), dtype=float)
    for a, b in zip(seq[:-1], seq[1:], strict=False):
        counts[int(a), int(b)] += 1.0
    out: dict[int, float] = {}
    for i in range(n_states):
        total = counts[i].sum()
        out[i] = float(counts[i, i] / total) if total > 0 else float("nan")
    return out


@dataclass(frozen=True, slots=True)
class PersistenceDiagnostic:
    stride: int
    per_bar_pstay: dict[str, float]   # label -> P(stay) counted every bar
    stride_pstay: dict[str, float]    # label -> P(stay) counted stride apart
    inflated: bool                    # per-bar materially stickier than stride
    message: str
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stride": self.stride,
            "per_bar_pstay": self.per_bar_pstay,
            "stride_pstay": self.stride_pstay,
            "inflated": self.inflated,
            "message": self.message,
            **self.stats,
        }


def persistence_diagnostic(
    model: RegimeModel,
    features: pd.DataFrame,
    *,
    stride: int = DEFAULT_STRIDE,
    warn_threshold: float = INFLATION_WARN_THRESHOLD,
) -> PersistenceDiagnostic:
    """Compare per-bar vs stride-sampled state persistence for a fitted model."""
    labels = model.labels
    n_states = model.n_states
    states = model.decode(features)

    per_bar = _diag_pstay(states, n_states)
    strided = _diag_pstay(states[::stride], n_states)

    per_bar_lbl = {labels[i].value: per_bar[i] for i in range(n_states)}
    stride_lbl = {labels[i].value: strided[i] for i in range(n_states)}

    # Compare on states present in both samples.
    gaps = [
        per_bar[i] - strided[i]
        for i in range(n_states)
        if not (np.isnan(per_bar[i]) or np.isnan(strided[i]))
    ]
    mean_gap = float(np.mean(gaps)) if gaps else float("nan")
    inflated = bool(gaps) and mean_gap > warn_threshold

    if inflated:
        message = (
            f"persistence likely inflated by feature autocorrelation: "
            f"per-bar diagonal exceeds stride-{stride} by mean {mean_gap:.3f} "
            f"(> {warn_threshold:.2f}); only the stride-sampled figure is honest"
        )
    else:
        message = (
            f"persistence within tolerance (per-bar vs stride-{stride} "
            f"mean gap {mean_gap:.3f})"
        )

    return PersistenceDiagnostic(
        stride=stride,
        per_bar_pstay=per_bar_lbl,
        stride_pstay=stride_lbl,
        inflated=inflated,
        message=message,
        stats={"mean_diag_gap": mean_gap},
    )
