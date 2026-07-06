"""
Public Brain API: ``predict_regime(bucket_id, symbol, ...)``.

This is what BucketRunner calls per candidate symbol each tick. Reads
the latest fitted model for (bucket, symbol) from the DB, fetches
fresh feature bars from a MarketData adapter, and runs
``predict_latest``. Persists one ``RegimeSnapshot`` row per inference.

Caching: predictions are cached per (bucket_id, symbol,
inference_window_start) to avoid re-running the HMM on every tick
within the same bar window. The cache lives in-process; restarts
re-warm by re-reading the model from the DB.

Per-symbol miss handling: if no model exists for (bucket, symbol),
``predict_regime`` optionally falls back to the bucket's broad-market
model (``symbol=MARKET_SENTINEL``), controlled by
``RegimeConfig.fallback_to_market``. If that also misses, returns
None and emits a ``regime_model_missing`` log line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.core.alerts import send_alert
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
from src.shared.regime.store import MARKET_SENTINEL, load_latest_for_symbol

_log = get_logger("shared.regime.brain")


class RegimeConfig(BaseModel):
    """Validated shape of ``regime.yaml`` per bucket.

    Fields:
        symbols: list of symbols to train and infer per-coin models for.
            When empty, the runtime (retrain_job + BucketRunner) falls
            back to the bucket's scanner universe. ``proxy_symbol`` is
            still accepted for backward compatibility and is used as the
            broad-market model's symbol.
        fallback_to_market: when a symbol has no per-coin model, defer
            to the bucket's broad-market regime (trained on
            ``proxy_symbol`` under ``symbol=MARKET_SENTINEL``).
    """

    enabled: bool = True
    proxy_symbol: str = "BTCUSD"
    tf: str
    training_window_days: int = Field(ge=30)
    inference_lookback_bars: int = Field(ge=50)
    retrain_cadence: str = "weekly"  # 'daily' | 'weekly' | 'manual'
    symbols: list[str] = Field(default_factory=list)
    fallback_to_market: bool = True

    # --- Markov 2.0 detection-layer knobs (retrain-time; safe defaults) ---
    # Baum-Welch finds local maxima; fit this many seeds and keep the best
    # log-likelihood. 1 == legacy single fit (random_state=7).
    n_restarts: int = Field(default=1, ge=1)
    # Verify each fitted model before it ships; reject + keep prior on fail.
    verify_labels: bool = True
    # Minimum mean-log-return separation required between adjacent regimes.
    min_mean_gap: float = Field(default=0.0005, ge=0.0)
    # Minimum Viterbi occupancy each state must hold (degenerate-state guard).
    min_state_occupancy: float = Field(default=0.03, ge=0.0)
    # Stride (bars) for the per-bar vs stride-sampled persistence diagnostic.
    persistence_diag_stride: int = Field(default=14, ge=1)


def load_regime_config(path: Path) -> RegimeConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RegimeConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _CacheKey:
    bucket_id: str
    symbol: str
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
    symbol: str,
    config: RegimeConfig,
    data: MarketData,
    clock: Clock | None = None,
) -> RegimePrediction | None:
    """Return the current regime for (bucket, symbol), or None.

    Resolution order:
      1. Per-symbol model in `regime_model` keyed by (bucket_id, symbol).
      2. Fallback to broad-market model (symbol=MARKET_SENTINEL) if
         ``config.fallback_to_market`` is True.
      3. None — caller treats as "no prediction" (sizer falls back to
         multiplier 1.0).

    Persists a ``RegimeSnapshot`` row per successful inference (keyed by
    the symbol actually used — i.e. MARKET_SENTINEL on fallback).
    Emits ``REGIME_CHANGE`` when the new label differs from the previous
    snapshot for the same (bucket, symbol).
    """
    if not config.enabled:
        return None

    clk = clock or RealClock()
    now = clk.now()
    cache_key = _CacheKey(
        bucket_id=bucket_id,
        symbol=symbol,
        window_start_iso=_window_start(now, config.tf).isoformat(),
    )
    if cache_key in _cache:
        return _cache[cache_key]

    # 1) Try per-symbol model first.
    with session_scope() as session:
        loaded = load_latest_for_symbol(session, bucket_id, symbol)
    used_symbol = symbol

    # 2) Fallback to broad-market model if requested + per-coin missed.
    if loaded is None and config.fallback_to_market:
        with session_scope() as session:
            loaded = load_latest_for_symbol(session, bucket_id, MARKET_SENTINEL)
        if loaded is not None:
            used_symbol = MARKET_SENTINEL

    if loaded is None:
        _log.warning(
            "regime_model_missing", bucket_id=bucket_id, symbol=symbol
        )
        return None
    version, model = loaded

    # Fetch OHLCV for the symbol actually being labelled (per-coin path)
    # or for the broad-market proxy (fallback path). For the broad-market
    # case, prefer ``config.proxy_symbol``.
    fetch_symbol = (
        config.proxy_symbol if used_symbol == MARKET_SENTINEL else symbol
    )
    try:
        # +1 then drop the last bar: exchanges return the in-progress
        # candle, and a regime label computed from a minutes-old bar (near
        # zero return / volume) is noise — worse, it gets cached for the
        # whole bar window. Label only fully-closed bars.
        raw_bars = data.get_ohlcv(
            fetch_symbol, config.tf, limit=config.inference_lookback_bars + 1
        )
    except Exception:
        _log.warning(
            "regime_inference_fetch_failed",
            bucket_id=bucket_id,
            symbol=fetch_symbol,
            exc_info=True,
        )
        raw_bars = None

    if raw_bars and len(raw_bars) > 1:
        raw_bars = raw_bars[:-1]

    if not raw_bars:
        _log.warning(
            "regime_features_unavailable",
            bucket_id=bucket_id,
            symbol=fetch_symbol,
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

    # Persist + emit REGIME_CHANGE if the label flipped vs the last
    # snapshot for this same (bucket, used_symbol).
    with session_scope() as session:
        prev = session.execute(
            select(RegimeSnapshot)
            .where(
                RegimeSnapshot.bucket_id == bucket_id,
                RegimeSnapshot.symbol == used_symbol,
            )
            .order_by(RegimeSnapshot.ts.desc())
            .limit(1)
        ).scalar_one_or_none()

        session.add(
            RegimeSnapshot(
                bucket_id=bucket_id,
                symbol=used_symbol,
                regime=pred.regime,
                state_probabilities={
                    k.value: round(v, 6) for k, v in pred.state_probabilities.items()
                },
                model_version=version,
                signal=round(pred.signal, 6),
            )
        )
        if prev is None or prev.regime != pred.regime:
            session.add(
                AuditLog(
                    strategy_id=bucket_id,
                    event_type=AuditEventType.REGIME_CHANGE,
                    message=(
                        f"{used_symbol} regime "
                        f"{prev.regime.value if prev else 'none'} -> "
                        f"{pred.regime.value}"
                    ),
                    payload={
                        "bucket_id": bucket_id,
                        "symbol": used_symbol,
                        "regime": pred.regime.value,
                        "probabilities": {
                            k.value: round(v, 6)
                            for k, v in pred.state_probabilities.items()
                        },
                        "model_version": version,
                    },
                )
            )
            if prev is not None:
                send_alert(
                    f"[{bucket_id}] {used_symbol} regime: "
                    f"{prev.regime.value} -> {pred.regime.value}"
                )

    _cache[cache_key] = pred
    _log.info(
        "regime_predicted",
        bucket_id=bucket_id,
        symbol=symbol,
        used_symbol=used_symbol,
        regime=pred.regime.value,
        model_version=version,
    )
    return pred


def clear_cache() -> None:
    """Test helper. Production code does not call this."""
    _cache.clear()
