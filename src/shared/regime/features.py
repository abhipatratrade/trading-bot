"""
Feature pipeline for the regime HMM.

Pure functions: input a price DataFrame indexed by datetime, output a
feature DataFrame the HMM can fit / predict on. No I/O.

We keep the feature set minimal — adding features inflates HMM
parameters and risks overfitting on the ~10 years of crypto history
available. Default features:

- ``log_return``        — log(close / close_prev)
- ``realised_vol``      — rolling std of log returns over ``vol_window``
- ``volume_zscore``     — rolling z-score of log(volume + 1)

These three give the HMM enough signal to distinguish bear (negative
drift + high vol), neutral (low drift), and bull (positive drift) regimes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_VOL_WINDOW: int = 14
DEFAULT_VOLUME_WINDOW: int = 30
DEFAULT_FEATURES: tuple[str, ...] = ("log_return", "realised_vol", "volume_zscore")


def compute_features(
    bars: pd.DataFrame,
    *,
    vol_window: int = DEFAULT_VOL_WINDOW,
    volume_window: int = DEFAULT_VOLUME_WINDOW,
    feature_columns: tuple[str, ...] = DEFAULT_FEATURES,
) -> pd.DataFrame:
    """Compute features from OHLCV bars.

    Args:
        bars: DataFrame with columns at least ``close`` and ``volume``,
            indexed by datetime.
        vol_window: rolling window for realised volatility.
        volume_window: rolling window for the volume z-score.
        feature_columns: subset to return. Defaults to all three.

    Returns:
        DataFrame indexed like ``bars`` with the requested feature columns.
        NaN rows from the warm-up windows are dropped before returning.
    """
    missing = {"close", "volume"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    if bars.empty:
        raise ValueError("bars is empty")

    out = pd.DataFrame(index=bars.index)
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)

    log_return = np.log(close / close.shift(1))
    out["log_return"] = log_return
    out["realised_vol"] = log_return.rolling(vol_window).std()

    log_vol = np.log(volume + 1.0)
    vol_mean = log_vol.rolling(volume_window).mean()
    vol_std = log_vol.rolling(volume_window).std()
    out["volume_zscore"] = (log_vol - vol_mean) / vol_std

    out = out[list(feature_columns)].dropna()
    if out.empty:
        raise ValueError(
            "no rows survived after rolling-window warm-up; "
            "supply more bars or shorten the windows"
        )
    return out
