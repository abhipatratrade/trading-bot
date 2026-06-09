"""
APScheduler-based background job runner.

Jobs:
    - Nightly trade export (Parquet + CSV, optional GDrive upload)
    - Reconciler sweep (every 5 minutes)
"""

from __future__ import annotations

import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler

from src.core.alerts import send_alert
from src.core.export import export_trades, upload_to_gdrive
from src.core.logging import configure_logging, get_logger
from src.shared.bucket import load_buckets
from src.shared.regime.brain import load_regime_config
from src.shared.regime.retrain_job import retrain_bucket

_log = get_logger("scheduler")


def _nightly_export() -> None:
    """Export yesterday's trades and optionally upload to GDrive."""
    _log.info("nightly_export_start")
    try:
        path = export_trades()
        if path:
            upload_to_gdrive(path)
            send_alert(f"Nightly export done: {path.name}")
        else:
            _log.info("nightly_export_no_trades")
    except Exception:
        _log.exception("nightly_export_failed")
        send_alert("Nightly export FAILED — check logs")


def _make_retrain_callable(bucket_id: str):
    def _job() -> None:
        _log.info("regime_retrain_start", bucket_id=bucket_id)
        try:
            version = retrain_bucket(bucket_id)
            send_alert(f"Regime retrained for {bucket_id}: {version}")
        except Exception:
            _log.exception("regime_retrain_failed", bucket_id=bucket_id)
            send_alert(f"Regime retrain FAILED for {bucket_id}")

    return _job


def _register_regime_retrains(scheduler: BlockingScheduler) -> None:
    """Register one retrain job per enabled bucket with a brain enabled.

    Cadence comes from each bucket's regime.yaml. Daily/weekly/manual are
    supported; manual cadence means no automatic job.
    """
    for bucket in load_buckets():
        if not bucket.config.enabled:
            continue
        try:
            cfg = load_regime_config(bucket.regime_yaml_path)
        except FileNotFoundError:
            continue
        if not cfg.enabled:
            continue

        if cfg.retrain_cadence == "daily":
            scheduler.add_job(
                _make_retrain_callable(bucket.id),
                "cron",
                hour=2,
                minute=0,
                id=f"retrain_{bucket.id}",
                replace_existing=True,
            )
        elif cfg.retrain_cadence == "weekly":
            scheduler.add_job(
                _make_retrain_callable(bucket.id),
                "cron",
                day_of_week="mon",
                hour=2,
                minute=0,
                id=f"retrain_{bucket.id}",
                replace_existing=True,
            )
        # "manual" → no job.


def main() -> None:
    configure_logging()
    _log.info("scheduler_starting")

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        _nightly_export,
        "cron",
        hour=0,
        minute=30,
        id="nightly_export",
        replace_existing=True,
    )

    _register_regime_retrains(scheduler)

    def _shutdown(signum: int, frame: object) -> None:
        _log.info("scheduler_shutting_down", signal=signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _log.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])
    scheduler.start()


if __name__ == "__main__":
    main()
