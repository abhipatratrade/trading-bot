"""
State→label mapping for the HMM.

After fitting, the model's hidden states are integers 0..N-1 with no
semantic meaning. We map them to MarketRegime labels by sorting on
the first feature dimension's mean (``log_return``):

- N=3 (full + diag tiers): lowest mean → BEAR, middle → NEUTRAL,
  highest → BULL.
- N=2 (low-data tier): lowest mean → BEAR, highest → BULL. No NEUTRAL
  label is produced. The sizer's ``regime_multipliers`` dict still
  needs a value for NEUTRAL but it just isn't reachable for 2-state
  symbols.

For N>3 the mapping is undefined — we don't support 5-state models
yet (Decision 014).
"""

from __future__ import annotations

import numpy as np

from src.core.models import MarketRegime

_THREE_STATE_LABELS: tuple[MarketRegime, ...] = (
    MarketRegime.BEAR,
    MarketRegime.NEUTRAL,
    MarketRegime.BULL,
)

_TWO_STATE_LABELS: tuple[MarketRegime, ...] = (
    MarketRegime.BEAR,
    MarketRegime.BULL,
)


def label_states_by_mean_return(means: np.ndarray) -> list[MarketRegime]:
    """Map state index → MarketRegime by sorting on mean log return.

    Args:
        means: shape (n_states, n_features). Column 0 must be the
            log-return feature (project convention enforced by
            ``compute_features``).

    Returns:
        A list of length n_states where ``out[i]`` is the label for
        state ``i``.

    Raises:
        ValueError: if n_states is not 2 or 3.
    """
    if means.ndim != 2:
        raise ValueError(f"means must be 2D, got shape {means.shape}")
    n_states = means.shape[0]
    if n_states == 3:
        label_pool: tuple[MarketRegime, ...] = _THREE_STATE_LABELS
    elif n_states == 2:
        label_pool = _TWO_STATE_LABELS
    else:
        raise ValueError(
            f"n_states must be 2 or 3, got {n_states}"
        )

    order = np.argsort(means[:, 0])  # ascending
    labels: list[MarketRegime | None] = [None] * n_states
    for sorted_idx, raw_idx in enumerate(order):
        labels[int(raw_idx)] = label_pool[sorted_idx]
    assert all(lab is not None for lab in labels)
    return [lab for lab in labels if lab is not None]
