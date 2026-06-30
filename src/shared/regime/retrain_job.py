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

Data source: Binance Futures public klines. We translate each
Delta-format ``bucket_symbol`` (e.g. BTCUSD) to its Binance equivalent
(BTCUSDT) via the ``symbol_mapping`` table. Binance has ~6 years of
daily history vs Delta India's ~1090 days — see the per-coin regime
plan for context.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import select

from src.core.alerts import send_alert
from src.core.clock import RealClock
from src.core.db import session_scope
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog, SymbolMapping
from src.data_sources.binance import BinanceData
from src.shared.bucket import Market, load_bucket, load_buckets
from src.shared.regime.brain import RegimeConfig, load_regime_config
from src.shared.regime.diagnostics import persistence_diagnostic
from src.shared.regime.features import compute_features
from src.shared.regime.hmm_model import RegimeModel
from src.shared.regime.store import MARKET_SENTINEL, save_model
from src.shared.regime.verify import VerifyResult, verify_model

_MIN_TRAIN_BARS: int = 150
_TIER_FULL_MIN: int = 700
_TIER_DIAG_MIN: int = 350

_log = get_logger("shared.regime.retrain")


def _json_safe(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/Inf) with None.

    Verification and diagnostic stats can carry NaN (e.g. a state that never
    transitions out in the stride-sampled path). Postgres JSONB rejects
    NaN/Infinity, so we scrub them before they reach ``extra`` / audit
    ``payload`` columns.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


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
# Data fetch (Binance Futures public klines)
# ---------------------------------------------------------------------------
def _fetch_bars(data: BinanceData, symbol: str, tf: str, limit: int):
    """Fetch OHLCV. Returns [] on errors (caller skips the symbol).

    ``limit`` is capped at 1500 by Binance per request; the bot's
    default training window (1100 daily bars) fits in one call.
    """
    try:
        return data.get_ohlcv(symbol, tf, limit=min(limit, 1500))
    except Exception:
        _log.warning("retrain_fetch_failed", symbol=symbol, exc_info=True)
        return []


def _delta_to_binance(delta_symbol: str) -> str | None:
    """Translate a Delta India symbol (e.g. BTCUSD) to its Binance
    Futures equivalent (e.g. BTCUSDT) via ``symbol_mapping``."""
    with session_scope() as session:
        row = session.execute(
            select(SymbolMapping).where(
                SymbolMapping.delta_symbol == delta_symbol
            )
        ).scalar_one_or_none()
        return row.binance_symbol if row and row.binance_symbol else None


# ---------------------------------------------------------------------------
# Public — single-symbol retrain
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RetrainResult:
    bucket_id: str
    symbol: str
    # "trained" | "skipped_low_data" | "fetch_failed" | "rejected_verification"
    status: str
    version: str | None = None
    tier: str | None = None
    n_bars: int = 0


def _retrain_one(
    *,
    bucket_id: str,
    bucket_symbol: str,
    fetch_symbol: str,
    cfg: RegimeConfig,
    data: BinanceData,
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
        n_restarts=cfg.n_restarts,
    ).fit(features)

    # FIX 1 (adapted): persistence/autocorrelation diagnostic. Reporting only
    # — it never blocks a model from shipping; it lands in ``extra`` + logs.
    diag = persistence_diagnostic(
        model, features, stride=cfg.persistence_diag_stride
    )
    if diag.inflated:
        _log.warning(
            "regime_persistence_inflated",
            bucket_id=bucket_id,
            symbol=bucket_symbol,
            message=diag.message,
        )

    # FIX 2: verify the freshly fitted model. On failure we do NOT save — the
    # previously-saved model stays the latest, so the bucket never ends up
    # with no regime. Audit + alert so the rejection is forensically visible.
    verify: VerifyResult | None = None
    if cfg.verify_labels:
        verify = verify_model(
            model,
            features,
            min_mean_gap=cfg.min_mean_gap,
            min_state_occupancy=cfg.min_state_occupancy,
        )
        if not verify.passed:
            _log.warning(
                "regime_model_rejected",
                bucket_id=bucket_id,
                symbol=bucket_symbol,
                tier=tier.name,
                n_bars=n_bars,
                reasons=verify.reasons,
            )
            with session_scope() as session:
                session.add(
                    AuditLog(
                        strategy_id=bucket_id,
                        event_type=AuditEventType.REGIME_MODEL_REJECTED,
                        message=(
                            f"{bucket_symbol} regime model REJECTED "
                            f"({tier.name}, {n_bars} bars), prior model kept: "
                            f"{'; '.join(verify.reasons)}"
                        ),
                        payload=_json_safe(
                            {
                                "bucket_id": bucket_id,
                                "symbol": bucket_symbol,
                                "fetch_symbol": fetch_symbol,
                                "tier": tier.name,
                                "n_bars": n_bars,
                                "verify": verify.as_dict(),
                                "persistence_diag": diag.as_dict(),
                            }
                        ),
                    )
                )
            send_alert(
                f"⚠️ [{bucket_id}] {bucket_symbol} regime model rejected "
                f"(kept prior): {verify.reasons[0]}"
            )
            return RetrainResult(
                bucket_id=bucket_id,
                symbol=bucket_symbol,
                status="rejected_verification",
                tier=tier.name,
                n_bars=n_bars,
            )

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
            extra=_json_safe(
                {
                    "tier": tier.name,
                    "fetch_symbol": fetch_symbol,
                    "n_bars": n_bars,
                    "feature_window_start": bars.index[0].isoformat(),
                    "feature_window_end": bars.index[-1].isoformat(),
                    "n_restarts": cfg.n_restarts,
                    "chosen_seed": model.chosen_seed,
                    "log_likelihood": model.log_likelihood,
                    "verify": verify.as_dict() if verify is not None else None,
                    "persistence_diag": diag.as_dict(),
                }
            ),
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
    # Per-coin models live under their own symbol. Delta-format symbols
    # in regime.yaml (e.g. BTCUSD) are translated to Binance symbols
    # (e.g. BTCUSDT) for the OHLCV fetch; storage still keys on the
    # Delta-format ``bucket_symbol`` so ``brain.predict_regime`` finds
    # the right model at inference time.
    symbols_to_train: list[tuple[str, str]] = []   # (bucket_symbol, fetch_symbol)
    market_fetch = _delta_to_binance(cfg.proxy_symbol)
    if market_fetch is None:
        _log.warning(
            "regime_train_missing_binance_mapping",
            bucket_id=bucket_id,
            delta_symbol=cfg.proxy_symbol,
            note="market fallback model will be skipped",
        )
    else:
        symbols_to_train.append((MARKET_SENTINEL, market_fetch))
    for sym in cfg.symbols:
        fetch = _delta_to_binance(sym)
        if fetch is None:
            _log.warning(
                "regime_train_missing_binance_mapping",
                bucket_id=bucket_id,
                delta_symbol=sym,
            )
            continue
        symbols_to_train.append((sym, fetch))

    results: list[RetrainResult] = []
    data = BinanceData()
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
        rejected=sum(
            1 for r in results if r.status == "rejected_verification"
        ),
    )
    return results


# ---------------------------------------------------------------------------
# Scheduling — which buckets to retrain
# ---------------------------------------------------------------------------
# Per Decision 020 the retrain runs on the Mumbai VM (a systemd timer),
# not the Railway scheduler: Binance Futures geo-blocks Railway's region,
# so every Railway-side retrain fetch_failed. The VM reaches Binance fine.
def _cadence_due_today(cadence: str, now: datetime) -> bool:
    """Whether a bucket with ``cadence`` should retrain on ``now`` (UTC)."""
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return now.weekday() == 0  # Monday
    return False  # "manual" / unknown → never auto-runs


def retrain_enabled_buckets(*, due_only: bool) -> dict[str, list[RetrainResult]]:
    """Retrain every enabled crypto bucket whose regime brain is enabled.

    When ``due_only`` is True a bucket retrains only if its
    ``retrain_cadence`` is due today (daily always; weekly on Mondays;
    manual never) — so a single daily timer honours per-bucket cadence.
    """
    now = RealClock().now()
    out: dict[str, list[RetrainResult]] = {}
    for bucket in load_buckets():
        if not bucket.config.enabled or bucket.market != Market.CRYPTO:
            continue
        try:
            cfg = load_regime_config(bucket.regime_yaml_path)
        except FileNotFoundError:
            continue
        if not cfg.enabled:
            continue
        if due_only and not _cadence_due_today(cfg.retrain_cadence, now):
            _log.info(
                "regime_retrain_not_due",
                bucket_id=bucket.id,
                cadence=cfg.retrain_cadence,
            )
            continue
        out[bucket.id] = retrain_bucket(bucket.id)
    return out


def _alert_summary(bucket_id: str, results: list[RetrainResult]) -> None:
    """Telegram one-liner per retrained bucket (was the scheduler's job)."""
    trained = sum(1 for r in results if r.status == "trained")
    failed = sum(1 for r in results if r.status == "fetch_failed")
    skipped = sum(1 for r in results if r.status == "skipped_low_data")
    rejected = sum(1 for r in results if r.status == "rejected_verification")
    prefix = "⚠️ " if (failed or rejected) else ""
    send_alert(
        f"{prefix}Regime retrain {bucket_id}: "
        f"trained={trained} fetch_failed={failed} "
        f"skipped={skipped} rejected={rejected}"
    )


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Retrain bucket per-symbol HMM regime models"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--bucket", help="retrain a single bucket_id")
    target.add_argument(
        "--all", action="store_true", help="retrain every enabled bucket"
    )
    target.add_argument(
        "--due",
        action="store_true",
        help="retrain enabled buckets whose cadence is due today (timer mode)",
    )
    args = parser.parse_args()

    try:
        if args.bucket:
            results_by_bucket = {args.bucket: retrain_bucket(args.bucket)}
        else:
            results_by_bucket = retrain_enabled_buckets(due_only=args.due)
    except Exception:
        _log.exception("regime_retrain_failed")
        send_alert(
            "⚠️ Regime retrain FAILED — see VM logs "
            "(journalctl -u regime-retrain)"
        )
        raise

    if not results_by_bucket:
        _log.info("regime_retrain_nothing_due")
        print("No buckets due for retrain.")
        return

    for bucket_id, results in results_by_bucket.items():
        for r in results:
            print(
                f"  {r.bucket_id}/{r.symbol}  {r.status}  "
                f"version={r.version}  tier={r.tier}  n_bars={r.n_bars}"
            )
        _alert_summary(bucket_id, results)


if __name__ == "__main__":  # pragma: no cover
    main()
