"""
Public Brain API: ``predict_regime(bucket_id, ...)``.

This is what BucketRunner calls each tick. Reads the latest fitted model
from the DB, fetches fresh feature bars from a MarketData adapter, and
runs ``predict_latest``. Persists a ``RegimeSnapshot`` row.

Caching: predictions are cached per (bucket_id, inference_window_start)
to avoid re-running the HMM on every tick within the same window. The
cache lives in-process; restarts re-warm from the DB row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    RegimeSnapshot,
)
from src.data_sources.base import MarketData
from src.shared.regime.features import compute_features
from src.shared.regime.hmm_model import RegimePrediction
from src.shared.regime.store import load_latest_for_bucket

_log = get_logger("shared.regime.brain")


class RegimeConfig(BaseModel):
    """Validated shape of ``regime.yaml`` per bucket."""

    enabled: bool = True
    proxy_symbol: str
    tf: str
    training_window_days: int = Field(ge=30)
    inference_lookback_bars: int = Field(ge=50)
    retrain_cadence: str = "weekly"  # 'daily' | 'weekly' | 'manual'

    # When disabled, predict_regime returns None (sizer falls back to mult=1).
    # When enabled but no model exists yet, predict_regime returns None and
    # logs a warning; sizer still falls back to mult=1.


def load_regime_config(path: Path) -> RegimeConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RegimeConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _CacheKey:
    bucket_id: str
    window_start_iso: str


_cache: dict[_CacheKey, RegimePrediction] = {}


def _window_start(now: datetime, tf: str) -> datetime:
    """Truncate ``now`` to the start of the current bar."""
    if tf == "1d":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if tf == "1h":
        return now.replace(minute=0, second=0, microsecond=0)
    if tf == "5m":
        minute = (now.minute // 5) * 5
        return now.replace(minute=minute, second=0, microsecond=0)
    # Unknown TF — don't cache.
    return now


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
def predict_regime(
    *,
    bucket_id: str,
    config: RegimeConfig,
    data: MarketData,
    clock: Clock | None = None,
) -> RegimePrediction | None:
    """Return the current regime for a bucket, or None if disabled / unavailable.

    Persists a ``RegimeSnapshot`` row on each successful inference. Logs a
    ``REGIME_CHANGE`` audit row when the new label differs from the
    previous snapshot.
    """
    if not config.enabled:
        return None

    clk = clock or RealClock()
    now = clk.now()
    cache_key = _CacheKey(
        bucket_id=bucket_id, window_start_iso=_window_start(now, config.tf).isoformat()
    )
    if cache_key in _cache:
        return _cache[cache_key]

    with session_scope() as session:
        loaded = load_latest_for_bucket(session, bucket_id)
    if loaded is None:
        _log.warning("regime_model_missing", bucket_id=bucket_id)
        return None
    version, model = loaded

    # Pull recent OHLCV for the proxy symbol and convert to DataFrame.
    raw_bars = data.get_ohlcv(
        config.proxy_symbol, config.tf, limit=config.inference_lookback_bars
    )
    if not raw_bars:
        _log.warning(
            "regime_features_unavailable",
            bucket_id=bucket_id,
            symbol=config.proxy_symbol,
        )
        return None

    bars = pd.DataFrame(
        {
            "timestamp": [b.timestamp for b in raw_bars],
            "close": [float(b.close) for b in raw_bars],
            "volume": [float(b.volume) for b in raw_bars],
        }
    ).set_index("timestamp")

    features = compute_features(bars)
    pred = model.predict_latest(features, model_version=version)

    # Persist + emit REGIME_CHANGE if the label flipped.
    with session_scope() as session:
        prev = session.execute(
            select(RegimeSnapshot)
            .where(RegimeSnapshot.bucket_id == bucket_id)
            .order_by(RegimeSnapshot.ts.desc())
            .limit(1)
        ).scalar_one_or_none()

        session.add(
            RegimeSnapshot(
                bucket_id=bucket_id,
                regime=pred.regime,
                state_probabilities={
                    k.value: round(v, 6) for k, v in pred.state_probabilities.items()
                },
                model_version=version,
            )
        )
        if prev is None or prev.regime != pred.regime:
            session.add(
                AuditLog(
                    strategy_id=bucket_id,
                    event_type=AuditEventType.REGIME_CHANGE,
                    message=(
                        f"regime {prev.regime if prev else 'none'} → {pred.regime}"
                    ),
                    payload={
                        "bucket_id": bucket_id,
                        "regime": pred.regime.value,
                        "probabilities": {
                            k.value: round(v, 6)
                            for k, v in pred.state_probabilities.items()
                        },
                        "model_version": version,
                    },
                )
            )

    _cache[cache_key] = pred
    _log.info(
        "regime_predicted",
        bucket_id=bucket_id,
        regime=pred.regime.value,
        model_version=version,
    )
    return pred


def clear_cache() -> None:
    """Test helper. Production code does not call this."""
    _cache.clear()
