"""
Retention pruning for append-only observability tables (Phase 1c).

Every tick writes scanner/sizing/regime snapshot rows and audit events;
without pruning the tables grow unbounded. Nightly scheduler job deletes:

    scanner_snapshot / sizing_snapshot / regime_snapshot
        older than ``SNAPSHOT_RETENTION_DAYS`` (default 60)
    audit_log
        older than ``AUDIT_RETENTION_DAYS`` (default 180 — the audit log
        is the forensic record, House Rule #8, so it keeps 3× longer)

Trades and positions are never pruned here (Postgres is the source of
truth; the nightly Parquet export is the archive path).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete

from src.core.clock import Clock, RealClock
from src.core.config import get_settings
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditLog,
    RegimeSnapshot,
    ScannerSnapshot,
    SizingSnapshot,
)

_log = get_logger("core.retention")


def prune_old_rows(clock: Clock | None = None) -> dict[str, int]:
    """Delete expired snapshot/audit rows. Returns per-table delete counts.

    Each table is pruned in its own transaction so one failure doesn't
    roll back the others.
    """
    settings = get_settings()
    now = (clock or RealClock()).now()
    snapshot_cutoff = now - timedelta(days=settings.snapshot_retention_days)
    audit_cutoff = now - timedelta(days=settings.audit_retention_days)

    targets = [
        ("scanner_snapshot", ScannerSnapshot, ScannerSnapshot.ts, snapshot_cutoff),
        ("sizing_snapshot", SizingSnapshot, SizingSnapshot.ts, snapshot_cutoff),
        ("regime_snapshot", RegimeSnapshot, RegimeSnapshot.ts, snapshot_cutoff),
        ("audit_log", AuditLog, AuditLog.ts, audit_cutoff),
    ]

    counts: dict[str, int] = {}
    for name, model, ts_col, cutoff in targets:
        try:
            with session_scope() as session:
                result = session.execute(delete(model).where(ts_col < cutoff))
                counts[name] = result.rowcount or 0
        except Exception:
            _log.error("prune_failed", table=name, exc_info=True)
            counts[name] = -1  # signals failure to the caller

    _log.info("retention_prune_done", **counts)
    return counts
