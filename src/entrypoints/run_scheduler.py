"""
APScheduler-based background job runner (Railway service).

Jobs:
    - Nightly trade export (Parquet + CSV, optional GDrive upload)

NOTE: the regime retrain used to be registered here, but Binance Futures
geo-blocks Railway's region, so every Railway-side retrain fetch_failed.
It now runs on the Mumbai VM via the ``regime-retrain`` systemd timer
(see ``ops/regime-retrain.*`` and ``docs/runbook.md``) — Decision 020.
"""

from __future__ import annotations

import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler

from src.core.alerts import send_alert
from src.core.export import export_trades, upload_to_gdrive
from src.core.logging import configure_logging, get_logger

_log = get_logger("scheduler")


def _nightly_export() -> None:
    """Export yesterday's trades and optionally upload to GDrive."""
    _log.info("nightly_export_start")
    try:
        path = export_trades()
        if path:
            uploaded = upload_to_gdrive(path)
            if uploaded:
                send_alert(f"Nightly export done + uploaded to GDrive: {path.name}")
            else:
                send_alert(
                    f"Nightly export done LOCAL ONLY (GDrive upload skipped/failed): "
                    f"{path.name}"
                )
        else:
            _log.info("nightly_export_no_trades")
    except Exception:
        _log.exception("nightly_export_failed")
        send_alert("Nightly export FAILED — check logs")


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
