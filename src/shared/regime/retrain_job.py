"""
Per-bucket per-symbol HMM retrain entrypoint.

Invocation:
    python -m src.shared.regime.retrain_job --bucket longterm-crypto

Flow:
    1. Load bucket + regime.yaml.
    2. Build the symbol list = cfg.symbols (or just the broad-market
       fallback if empty).
    3. Always also train the broad-market model
       (``symbol=MARKET_SENTINEL`` using ``cfg.proxy_symbol`` as the
       OHLCV source). This is the fallback for low-data coins.
    4. For each symbol: fetch OHLCV, pick a tier from the bar count,
       fit, persist.
    5. Audit one row per (bucket, symbol) trained.

The bot continues to use the previously-saved model until
``brain.predict_regime`` runs next and finds a newer ``trained_at`` —
retrains are non-blocking.

Data source: Delta India LIVE. After the GCP VM migrates to
``asia-south1`` (Mumbai), this can switch back to Binance (more
history for liquid alts). Tracked in the plan as the prerequisite.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import httpx
import pandas as pd

from src.core.clock import RealClock
from src.core.db import session_scope
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog
from src.data_sources.delta_india import DeltaIndiaData
from src.shared.bucket import Market, load_bucket
from src.shared.regime.brain import RegimeConfig, load_regime_config
from src.shared.regime.features import compute_features
from src.shared.regime.hmm_model import RegimeModel
from src.shared.regime.store import MARKET_SENTINEL, save_model

# Delta India testnet only retains ~75 days of BTCUSD history. The LIVE
# endpoint has ~3 years of history and is unauthenticated (public
# market-data only), so it's safe to use regardless of TRADING_MODE.
_DELTA_LIVE_BASE_URL = "https://api.india.delta.exchange"

_MIN_TRAIN_BARS: int = 150
_TIER_FULL_MIN: int = 700
_TIER_DIAG_MIN: int = 350

_log = get_logger("shared.regime.retrain")


# ---------------------------------------------------------------------------
# Tier picker
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TrainTier:
    """Model architecture chosen for a given bar count."""

    n_states: int
    covariance_type: str
    feature_columns: tuple[str, ...]
    name: str


_TIER_FULL_3 = TrainTier(
    n_states=3,
    covariance_type="full",
    feature_columns=("log_return", "realised_vol", "volume_zscore"),
    name="3-state-full",
)
_TIER_DIAG_3 = TrainTier(
    n_states=3,
    covariance_type="diag",
    feature_columns=("log_return", "realised_vol", "volume_zscore"),
    name="3-state-diag",
)
_TIER_DIAG_2 = TrainTier(
    n_states=2,
    covariance_type="diag",
    feature_columns=("log_return", "realised_vol"),
    name="2-state-diag",
)


def pick_tier(n_bars: int) -> TrainTier | None:
    """Map bar count to a model architecture.

    Returns None when there is not enough data to train any tier
    (caller should skip the symbol).
    """
    if n_bars >= _TIER_FULL_MIN:
        return _TIER_FULL_3
    if n_bars >= _TIER_DIAG_MIN:
        return _TIER_DIAG_3
    if n_bars >= _MIN_TRAIN_BARS:
        return _TIER_DIAG_2
    return None


# ---------------------------------------------------------------------------
# Data fetch (Delta India LIVE)
# ---------------------------------------------------------------------------
def _open_delta_live() -> DeltaIndiaData:
    """A DeltaIndiaData client pointed at the LIVE base URL.

    Caller must call ``.close()``.
    """
    data = DeltaIndiaData()
    data._http = httpx.Client(
        base_url=_DELTA_LIVE_BASE_URL,
        timeout=20.0,
        headers={"User-Agent": "trading-bot-retrain/0.1.0"},
    )
    return data


def _fetch_bars(data: DeltaIndiaData, symbol: str, tf: str, limit: int):
    """Fetch OHLCV. Returns [] on errors (caller skips the symbol)."""
    try:
        return data.get_ohlcv(symbol, tf, limit=limit)
    except Exception:
        _log.warning("retrain_fetch_failed", symbol=symbol, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Public — single-symbol retrain
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RetrainResult:
    bucket_id: str
    symbol: str
    status: str          # "trained" | "skipped_low_data" | "fetch_failed"
    version: str | None = None
    tier: str | None = None
    n_bars: int = 0


def _retrain_one(
    *,
    bucket_id: str,
    bucket_symbol: str,
    fetch_symbol: str,
    cfg: RegimeConfig,
    data: DeltaIndiaData,
) -> RetrainResult:
    """Train and persist one regime model for (bucket_id, bucket_symbol).

    ``fetch_symbol`` is the symbol used for the OHLCV fetch (e.g.
    ``cfg.proxy_symbol`` when training the broad-market fallback).
    """
    raw_bars = _fetch_bars(data, fetch_symbol, cfg.tf, cfg.training_window_days)
    if not raw_bars:
        return RetrainResult(
            bucket_id=bucket_id, symbol=bucket_symbol, status="fetch_failed"
        )

    n_bars = len(raw_bars)
    tier = pick_tier(n_bars)
    if tier is None:
        _log.warning(
            "regime_train_skipped_low_data",
            bucket_id=bucket_id,
            symbol=bucket_symbol,
            fetch_symbol=fetch_symbol,
            n_bars=n_bars,
            need=_MIN_TRAIN_BARS,
        )
        return RetrainResult(
            bucket_id=bucket_id,
            symbol=bucket_symbol,
            status="skipped_low_data",
            n_bars=n_bars,
        )

    bars = pd.DataFrame(
        {
            "timestamp": [b.timestamp for b in raw_bars],
            "close": [float(b.close) for b in raw_bars],
            "volume": [float(b.volume) for b in raw_bars],
        }
    ).set_index("timestamp")

    features = compute_features(bars, feature_columns=tier.feature_columns)
    model = RegimeModel(
        feature_columns=list(features.columns),
        n_states=tier.n_states,
        covariance_type=tier.covariance_type,
    ).fit(features)

    now = RealClock().now()
    version = f"v{now.strftime('%Y%m%d_%H%M%S')}_{bucket_symbol}"

    with session_scope() as session:
        save_model(
            session,
            bucket_id=bucket_id,
            symbol=bucket_symbol,
            version=version,
            trained_at=now,
            model=model,
            extra={
                "tier": tier.name,
                "fetch_symbol": fetch_symbol,
                "n_bars": n_bars,
                "feature_window_start": bars.index[0].isoformat(),
                "feature_window_end": bars.index[-1].isoformat(),
            },
        )
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.REGIME_MODEL_RETRAINED,
                message=(
                    f"{bucket_symbol} regime model retrained "
                    f"({tier.name}, {n_bars} bars): {version}"
                ),
                payload={
                    "bucket_id": bucket_id,
                    "symbol": bucket_symbol,
                    "fetch_symbol": fetch_symbol,
                    "version": version,
                    "tier": tier.name,
                    "n_bars": n_bars,
                    "trained_at": now.isoformat(),
                },
            )
        )

    _log.info(
        "regime_retrained",
        bucket_id=bucket_id,
        symbol=bucket_symbol,
        version=version,
        tier=tier.name,
        n_bars=n_bars,
    )
    return RetrainResult(
        bucket_id=bucket_id,
        symbol=bucket_symbol,
        status="trained",
        version=version,
        tier=tier.name,
        n_bars=n_bars,
    )


# ---------------------------------------------------------------------------
# Public — bucket retrain (all symbols + broad-market fallback)
# ---------------------------------------------------------------------------
def retrain_bucket(bucket_id: str) -> list[RetrainResult]:
    """Train per-symbol HMMs for ``bucket_id`` plus the market fallback.

    Returns one RetrainResult per attempted symbol.
    """
    bucket = load_bucket(bucket_id)
    cfg = load_regime_config(bucket.regime_yaml_path)

    if bucket.market != Market.CRYPTO:
        raise NotImplementedError(
            f"Retrain not implemented for market={bucket.market}. "
            "Indian-market HMM lands when the Dhan data adapter ships."
        )

    # Always train the broad-market fallback under MARKET_SENTINEL.
    # Per-coin models live under their own symbol.
    symbols_to_train: list[tuple[str, str]] = []   # (bucket_symbol, fetch_symbol)
    symbols_to_train.append((MARKET_SENTINEL, cfg.proxy_symbol))
    for sym in cfg.symbols:
        symbols_to_train.append((sym, sym))

    results: list[RetrainResult] = []
    data = _open_delta_live()
    try:
        for bucket_symbol, fetch_symbol in symbols_to_train:
            results.append(
                _retrain_one(
                    bucket_id=bucket_id,
                    bucket_symbol=bucket_symbol,
                    fetch_symbol=fetch_symbol,
                    cfg=cfg,
                    data=data,
                )
            )
    finally:
        data.close()

    _log.info(
        "retrain_bucket_complete",
        bucket_id=bucket_id,
        n_symbols=len(results),
        trained=sum(1 for r in results if r.status == "trained"),
        skipped_low_data=sum(
            1 for r in results if r.status == "skipped_low_data"
        ),
        fetch_failed=sum(1 for r in results if r.status == "fetch_failed"),
    )
    return results


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Retrain a bucket's per-symbol HMM regime models"
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="bucket_id (e.g. longterm-crypto)",
    )
    args = parser.parse_args()
    results = retrain_bucket(args.bucket)
    for r in results:
        print(
            f"  {r.bucket_id}/{r.symbol}  {r.status}  "
            f"version={r.version}  tier={r.tier}  n_bars={r.n_bars}"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
