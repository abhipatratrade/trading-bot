"""
Per-bucket HMM retrain entrypoint.

Invocation:
    python -m src.shared.regime.retrain_job --bucket longterm-crypto

The scheduler service (``src/entrypoints/run_scheduler.py``) wires one
APScheduler job per bucket, on the cadence declared in the bucket's
``regime.yaml``.

Flow:
    1. Load bucket + regime.yaml.
    2. Pull historical OHLCV for the proxy symbol (training_window_days).
    3. Compute features.
    4. Fit RegimeModel.
    5. Persist via store.save_model with a fresh version string.
    6. Log audit row.

The bot continues to use the previously-saved model until ``brain.predict_regime``
is called next and finds a newer ``trained_at`` — so retrains are non-blocking.
"""

from __future__ import annotations

import argparse
from datetime import timedelta

import httpx
import pandas as pd

from src.core.clock import RealClock
from src.core.db import session_scope
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog
from src.data_sources.delta_india import DeltaIndiaData
from src.shared.bucket import Market, load_bucket
from src.shared.regime.brain import load_regime_config
from src.shared.regime.features import compute_features
from src.shared.regime.hmm_model import RegimeModel
from src.shared.regime.store import save_model

# Delta India testnet only retains ~75 days of BTCUSD history (it's a
# recently-launched testnet). For HMM training we need at least a few
# years of bars, which the LIVE public endpoint has. Live market-data
# endpoints are unauthenticated, so this is safe to use regardless of
# the bot's TRADING_MODE.
_DELTA_LIVE_BASE_URL = "https://api.india.delta.exchange"

_log = get_logger("shared.regime.retrain")


def retrain_bucket(bucket_id: str) -> str:
    """Fit and persist a new regime model for ``bucket_id``. Returns version."""
    bucket = load_bucket(bucket_id)
    cfg = load_regime_config(bucket.regime_yaml_path)

    if bucket.market != Market.CRYPTO:
        raise NotImplementedError(
            f"Retrain not implemented for market={bucket.market}. "
            "Indian-market HMM lands in Phase 3 (Dhan data adapter)."
        )

    # Use Delta India for training data: same exchange the bot uses at
    # inference, reachable from any host (Binance is geoblocked from the
    # GCP bot-worker). Override the HTTP client to point at LIVE since
    # testnet only retains ~75 days of history.
    data = DeltaIndiaData()
    try:
        data._http = httpx.Client(
            base_url=_DELTA_LIVE_BASE_URL,
            timeout=20.0,
            headers={"User-Agent": "trading-bot-retrain/0.1.0"},
        )
        raw_bars = data.get_ohlcv(
            cfg.proxy_symbol, cfg.tf, limit=cfg.training_window_days
        )
    finally:
        data.close()

    if len(raw_bars) < 100:
        raise RuntimeError(
            f"Not enough bars to fit HMM: got {len(raw_bars)}, need ≥ 100"
        )

    bars = pd.DataFrame(
        {
            "timestamp": [b.timestamp for b in raw_bars],
            "close": [float(b.close) for b in raw_bars],
            "volume": [float(b.volume) for b in raw_bars],
        }
    ).set_index("timestamp")

    features = compute_features(bars)
    model = RegimeModel(feature_columns=list(features.columns)).fit(features)

    now = RealClock().now()
    version = f"v{now.strftime('%Y%m%d_%H%M%S')}"

    with session_scope() as session:
        save_model(
            session,
            bucket_id=bucket_id,
            version=version,
            trained_at=now,
            model=model,
            extra={
                "n_bars": int(len(bars)),
                "feature_window_start": bars.index[0].isoformat(),
                "feature_window_end": bars.index[-1].isoformat(),
            },
        )
        session.add(
            AuditLog(
                strategy_id=bucket_id,
                event_type=AuditEventType.REGIME_MODEL_RETRAINED,
                message=f"regime model retrained: {version}",
                payload={
                    "bucket_id": bucket_id,
                    "version": version,
                    "n_bars": int(len(bars)),
                    "trained_at": now.isoformat(),
                },
            )
        )

    _log.info("regime_retrained", bucket_id=bucket_id, version=version)
    return version


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Retrain a bucket's HMM regime model"
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="bucket_id (e.g. longterm-crypto)",
    )
    args = parser.parse_args()
    retrain_bucket(args.bucket)


# Suppress unused-import warning while keeping the timedelta import handy
# for downstream call sites that compute windows from cfg.training_window_days.
_ = timedelta


if __name__ == "__main__":  # pragma: no cover
    main()
