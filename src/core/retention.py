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

AUDIT ROWS ARE NEVER DELETED AHEAD OF THEIR ARCHIVE (2026-08-03). The audit
log is the only forensic tool when something goes wrong, and this job used to
hard-delete it on a timer with no copy behind it — the nightly export covered
``trade`` and nothing else, and the Drive mirror had never once run. So the
cutoff is now the EARLIER of the retention age and the archive watermark
(``core/export.audit_archived_through``). A stalled archive stalls the
deletion, which is the correct direction to fail: the table grows and the
operator gets paged, instead of the record quietly disappearing.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

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

# Returned per table when the prune could not run. Distinct from 0 ("ran, had
# nothing to do") because only one of the two is worth waking someone for.
FAILED = -1
BLOCKED = -2


def audit_cutoff(
    now: datetime, retention_days: int, archived_through: object
) -> datetime | None:
    """How far back audit rows may be deleted. PURE.

    ``None`` means delete nothing: either nothing has ever been archived, or
    the archive has not yet caught up to the retention horizon. Returns the
    EARLIER of the two bounds, so the archive can only ever slow the deletion
    down, never speed it up.
    """
    by_age = now - timedelta(days=retention_days)
    if archived_through is None:
        return None
    # The watermark names a whole UTC day that is safely archived, so rows are
    # deletable up to the END of it — midnight the following day.
    by_archive = datetime.combine(
        archived_through + timedelta(days=1), time.min, tzinfo=now.tzinfo
    )
    cutoff = min(by_age, by_archive)
    return None if cutoff <= datetime.min.replace(tzinfo=now.tzinfo) else cutoff


def prune_old_rows(clock: Clock | None = None) -> dict[str, int]:
    """Delete expired snapshot/audit rows. Returns per-table delete counts.

    Each table is pruned in its own transaction so one failure doesn't
    roll back the others. ``FAILED`` signals an error, ``BLOCKED`` signals
    audit rows held back because the archive has not reached them.
    """
    from src.core.export import audit_archived_through

    settings = get_settings()
    now = (clock or RealClock()).now()
    snapshot_cutoff = now - timedelta(days=settings.snapshot_retention_days)

    counts: dict[str, int] = {}
    snapshots = [
        ("scanner_snapshot", ScannerSnapshot, ScannerSnapshot.ts),
        ("sizing_snapshot", SizingSnapshot, SizingSnapshot.ts),
        ("regime_snapshot", RegimeSnapshot, RegimeSnapshot.ts),
    ]
    for name, model, ts_col in snapshots:
        try:
            with session_scope() as session:
                result = session.execute(
                    delete(model).where(ts_col < snapshot_cutoff)
                )
                counts[name] = result.rowcount or 0
        except Exception:
            _log.error("prune_failed", table=name, exc_info=True)
            counts[name] = FAILED

    archived = audit_archived_through()
    cutoff = audit_cutoff(now, settings.audit_retention_days, archived)
    if cutoff is None:
        _log.warning(
            "audit_prune_blocked_on_archive",
            archived_through=str(archived) if archived else None,
            hint="nothing is deleted until the nightly Drive archive catches up",
        )
        counts["audit_log"] = BLOCKED
    else:
        try:
            with session_scope() as session:
                result = session.execute(delete(AuditLog).where(AuditLog.ts < cutoff))
                counts["audit_log"] = result.rowcount or 0
        except Exception:
            _log.error("prune_failed", table="audit_log", exc_info=True)
            counts["audit_log"] = FAILED

    _log.info("retention_prune_done", **counts)
    return counts
