"""
APScheduler-based background job runner (Railway service).

Jobs:
    - Nightly trade export (Parquet + CSV, optional GDrive upload)
    - Heartbeat watch (dead-man's switch): pages when the bot-worker's
      heartbeat row goes stale. Runs on Railway — infrastructure
      independent of the GCP VM, so a dead VM cannot silence its own
      watchdog.
    - Nightly retention prune: deletes expired scanner/sizing/regime
      snapshot rows (SNAPSHOT_RETENTION_DAYS) and audit_log rows
      (AUDIT_RETENTION_DAYS).

NOTE: the regime retrain used to be registered here, but Binance Futures
geo-blocks Railway's region, so every Railway-side retrain fetch_failed.
It now runs on the Mumbai VM via the ``regime-retrain`` systemd timer
(see ``ops/regime-retrain.*`` and ``docs/runbook.md``) — Decision 020.
"""

from __future__ import annotations

import signal
import sys
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from src.core.alerts import note_alert_recovery, send_alert, send_alert_dedup
from src.core.config import get_settings
from src.core.export import export_trades, upload_to_gdrive
from src.core.heartbeat import SERVICE_BOT_WORKER, last_beat, staleness
from src.core.logging import configure_logging, get_logger
from src.core.retention import prune_old_rows

_log = get_logger("scheduler")

_HEARTBEAT_ALERT_KEY = f"heartbeat_stale:{SERVICE_BOT_WORKER}"


def _heartbeat_watch() -> None:
    """Page when the bot-worker heartbeat is stale; ping once on recovery."""
    try:
        beat_at = last_beat(SERVICE_BOT_WORKER)
    except Exception:
        _log.exception("heartbeat_watch_db_error")
        return
    threshold = get_settings().heartbeat_stale_seconds
    stale, age = staleness(beat_at, datetime.now(tz=UTC), threshold)
    if stale:
        age_str = (
            f"{int(age // 60)}m{int(age % 60)}s old"
            if age is not None
            else "MISSING (never beat)"
        )
        _log.warning("heartbeat_stale", service=SERVICE_BOT_WORKER, age=age_str)
        send_alert_dedup(
            _HEARTBEAT_ALERT_KEY,
            f"🚨 DEAD-MAN'S SWITCH: {SERVICE_BOT_WORKER} heartbeat is "
            f"{age_str} (threshold {threshold}s).\n"
            f"Bot/VM may be DOWN — positions are stop-protected on the "
            f"exchange, but nothing is trading. Check: "
            f"journalctl -u bot-worker on the VM.",
        )
    else:
        note_alert_recovery(
            _HEARTBEAT_ALERT_KEY,
            f"✅ {SERVICE_BOT_WORKER} heartbeat recovered "
            f"(age {int(age)}s)" if age is not None else "✅ heartbeat recovered",
        )


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


def _nightly_prune() -> None:
    """Prune expired snapshot/audit rows; page only when a table fails."""
    counts = prune_old_rows()
    failed = [t for t, n in counts.items() if n < 0]
    if failed:
        send_alert(f"Retention prune FAILED for: {', '.join(failed)} — check logs")


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

    scheduler.add_job(
        _heartbeat_watch,
        "interval",
        minutes=2,
        id="heartbeat_watch",
        replace_existing=True,
    )

    scheduler.add_job(
        _nightly_prune,
        "cron",
        hour=1,
        minute=0,
        id="nightly_prune",
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
