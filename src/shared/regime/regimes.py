"""
State→label mapping for the HMM.

After fitting, the model's hidden states are integers 0..N-1 with no
semantic meaning. We map them to ``bear`` / ``neutral`` / ``bull`` by
sorting on the first feature dimension's mean (``log_return``):
- lowest mean       → BEAR
- middle mean       → NEUTRAL
- highest mean      → BULL

For N=3 this is unambiguous. For N>3 we collapse extra states into the
nearest of the three labels (preserved here for future 5-state work,
which is out of scope per the plan).
"""

from __future__ import annotations

import numpy as np

from src.core.models import MarketRegime

_THREE_STATE_LABELS: tuple[MarketRegime, ...] = (
    MarketRegime.BEAR,
    MarketRegime.NEUTRAL,
    MarketRegime.BULL,
)


def label_states_by_mean_return(means: np.ndarray) -> list[MarketRegime]:
    """Map state index → MarketRegime by sorting on mean log return.

    Args:
        means: shape (n_states, n_features). Column 0 must be the log-return
            feature (which is the project convention enforced by
            ``compute_features``).

    Returns:
        A list of length n_states where ``out[i]`` is the label for state ``i``.

    Raises:
        ValueError: if n_states != 3 (we hard-require 3 for v1).
    """
    if means.ndim != 2:
        raise ValueError(f"means must be 2D, got shape {means.shape}")
    n_states = means.shape[0]
    if n_states != 3:
        raise ValueError(
            f"n_states must be 3 for the v1 label mapping, got {n_states}"
        )

    order = np.argsort(means[:, 0])  # ascending
    labels: list[MarketRegime | None] = [None] * n_states
    for sorted_idx, raw_idx in enumerate(order):
        labels[int(raw_idx)] = _THREE_STATE_LABELS[sorted_idx]
    assert all(lab is not None for lab in labels)
    return [lab for lab in labels if lab is not None]
