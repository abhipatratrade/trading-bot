"""
APScheduler-based background job runner (Railway service).

Jobs:
    - Nightly archive: trades AND audit_log (Parquet + CSV, mirrored to
      Google Drive). The audit half is not optional garnish — retention
      hard-deletes audit rows at 180 days, and the prune below will not pass
      the watermark this job writes.
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
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from src.core import retention
from src.core.alerts import note_alert_recovery, send_alert, send_alert_dedup
from src.core.config import get_settings
from src.core.export import (
    export_audit_log,
    export_trades,
    mark_audit_archived,
    upload_to_gdrive,
)
from src.core.heartbeat import SERVICE_BOT_WORKER, last_beat, staleness
from src.core.logging import configure_logging, get_logger
from src.core.retention import prune_old_rows
from src.reporting import eod
from src.shared.market_calendar import IST, is_trading_day

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
    """Archive yesterday's trades and audit rows, and mirror them to Drive.

    The audit half gates retention: ``mark_audit_archived`` is called ONLY on a
    confirmed upload, and ``prune_old_rows`` refuses to delete past that mark.
    A day with no audit rows still advances the watermark — there is nothing to
    lose, and letting an idle day block the prune forever would be a slow leak.

    LOCAL ONLY is an ALERT, not a footnote. It read as a mild caveat for
    months while the mirror had in fact never run once: the container's disk is
    ephemeral, so local-only means the file is gone at the next deploy.
    """
    _log.info("nightly_export_start")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    try:
        done: list[str] = []
        local_only: list[str] = []

        trade_path = export_trades()
        if trade_path:
            target = done if upload_to_gdrive(trade_path) else local_only
            target.append(trade_path.name)

        audit_path = export_audit_log()
        audit_safe = False
        if audit_path is None:
            audit_safe = True  # nothing written that day — nothing to lose
        elif upload_to_gdrive(audit_path):
            done.append(audit_path.name)
            audit_safe = True
        else:
            local_only.append(audit_path.name)

        if audit_safe:
            mark_audit_archived(yesterday)

        if local_only:
            send_alert(
                f"🚨 Nightly archive LOCAL ONLY — NOT in Drive: "
                f"{', '.join(local_only)}. This container's disk is ephemeral, "
                f"so these are lost on the next deploy, and the audit-log prune "
                f"is now BLOCKED until the mirror works. See docs/runbook.md."
            )
        elif done:
            send_alert(f"Nightly archive uploaded to Drive: {', '.join(done)}")
        else:
            _log.info("nightly_export_nothing_to_archive", date=str(yesterday))
    except Exception:
        _log.exception("nightly_export_failed")
        send_alert("Nightly archive FAILED — check logs")


def _eod_report() -> None:
    """Build and send the end-of-day session postmortem (Decision 033).

    Runs at 10:15 UTC = 15:45 IST, after intraday-indian's 15:15 square-off and
    the 15:30 close, so what is still open genuinely IS carried overnight.
    Skips non-trading days: a Sunday has no session to report on.
    """
    today = datetime.now(tz=IST).date()
    if not is_trading_day(today):
        _log.info("eod_report_skipped_non_trading_day", date=str(today))
        return
    try:
        report = eod.gather(today)
        eod.store(report)
        send_alert(eod.render_digest(report))
        _log.info("eod_report_done", date=str(today), quiet=report.quiet)
    except Exception:
        _log.exception("eod_report_failed")
        send_alert("EOD report FAILED — check logs (trading is unaffected)")


def _nightly_prune() -> None:
    """Prune expired snapshot/audit rows; page only when a table fails.

    BLOCKED is not a failure and does not page here — the archive job already
    alerted, and two messages about one cause is how alert fatigue starts.
    """
    counts = prune_old_rows()
    failed = [t for t, n in counts.items() if n == retention.FAILED]
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

    # 10:15 UTC = 15:45 IST — after the 15:15 square-off and the 15:30 close.
    scheduler.add_job(
        _eod_report,
        "cron",
        day_of_week="mon-fri",
        hour=10,
        minute=15,
        id="eod_report",
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
