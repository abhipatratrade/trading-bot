"""
Archive the audit-log backlog to Drive, oldest day first, and set the watermark.

The nightly job only ever handles yesterday. Everything written before the
archive existed — 2026-05-01 onward on the live database — has never left the
box, and ``core/retention.py`` deliberately refuses to prune ANY audit rows
until a contiguous archive reaches them. This script closes that gap.

    python -m scripts.archive_backfill --check     # credentials only, no writes
    python -m scripts.archive_backfill --dry-run   # what it would upload
    python -m scripts.archive_backfill             # do it

Safe to re-run: uploads replace same-named files rather than duplicating, and
the watermark only ever moves forward. Stops at the first day that fails to
upload, so the watermark can never claim more than is true.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from src.core.config import get_settings
from src.core.db import session_scope
from src.core.export import (
    audit_archived_through,
    export_audit_log,
    mark_audit_archived,
    upload_to_gdrive,
)
from src.core.logging import configure_logging
from src.core.models import AuditLog


def _oldest_audit_day() -> date | None:
    with session_scope() as session:
        oldest = session.execute(select(func.min(AuditLog.ts))).scalar()
    return oldest.astimezone(UTC).date() if oldest is not None else None


def _check() -> int:
    """Report configuration without uploading anything."""
    settings = get_settings()
    print(f"GDRIVE_FOLDER_ID set : {bool(settings.gdrive_folder_id)}")
    print(f"OAuth configured     : {settings.gdrive_oauth_configured}")
    print(f"Service account set  : {bool(settings.gdrive_service_account_json)}")
    print(f"gdrive_enabled       : {settings.gdrive_enabled}")
    try:
        import googleapiclient  # noqa: F401

        print("google libraries     : installed")
    except ImportError:
        print("google libraries     : MISSING (pip install -r requirements.txt)")
    print(f"watermark            : {audit_archived_through() or 'never archived'}")
    print(f"oldest audit day     : {_oldest_audit_day() or 'no audit rows'}")
    if not settings.gdrive_enabled:
        print("\nNot configured — see docs/runbook.md, 'Google Drive archive'.")
        return 1
    return 0


def main() -> int:
    # A Windows console defaults to cp1252, which cannot encode the em-dashes
    # and arrows in this module's own help text — the traceback then looks like
    # an archive failure rather than a console one.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Config report only.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--from",
        dest="from_date",
        type=date.fromisoformat,
        default=None,
        help="First UTC day to archive. Defaults to the oldest audit row.",
    )
    args = parser.parse_args()
    configure_logging()

    if args.check:
        return _check()

    settings = get_settings()
    if not settings.gdrive_enabled and not args.dry_run:
        print("Google Drive is not configured. Run with --check for details.")
        return 1

    watermark = audit_archived_through()
    # The first day that would leave NO gap below it. Anything later than this
    # is a partial run, and a partial run must not move the watermark: the
    # watermark asserts "everything up to here is archived", so advancing it
    # past un-archived days is precisely the data-loss bug the guard exists to
    # prevent — retention would immediately consider that history deletable.
    natural_start = (
        watermark + timedelta(days=1) if watermark else _oldest_audit_day()
    )
    start = args.from_date or natural_start
    if start is None:
        print("No audit rows to archive.")
        return 0
    leaves_gap = natural_start is not None and start > natural_start

    # Yesterday is the last COMPLETE UTC day; today is still being written.
    end = (datetime.now(UTC) - timedelta(days=1)).date()
    if start > end:
        print(f"Nothing to do — archived through {watermark}.")
        return 0

    print(f"Archiving audit_log {start} -> {end} ...")
    day = start
    last_ok: date | None = None
    while day <= end:
        moment = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        if args.dry_run:
            print(f"  {day}  (dry run)")
            last_ok = day
            day += timedelta(days=1)
            continue

        path = export_audit_log(moment)
        if path is None:
            print(f"  {day}  no rows — nothing to lose, day counts as archived")
            last_ok = day
        elif upload_to_gdrive(path):
            print(f"  {day}  uploaded {path.name}")
            last_ok = day
        else:
            # Stop rather than skip: a hole would make the watermark a lie.
            print(f"  {day}  UPLOAD FAILED — stopping here. Fix, then re-run.")
            break
        day += timedelta(days=1)

    if args.dry_run:
        print(f"\nDry run — watermark unchanged at {watermark or 'never archived'}.")
    elif last_ok is None:
        print("\nNothing archived — watermark unchanged.")
    elif leaves_gap:
        print(
            f"\nWatermark NOT moved (still {watermark or 'never archived'}).\n"
            f"--from {start} skipped everything from {natural_start}, so days "
            f"below it are still un-archived and must not be marked safe.\n"
            f"Re-run without --from to close the gap."
        )
    else:
        mark_audit_archived(last_ok, force=True)
        print(f"\nWatermark set to {last_ok}. Retention may now prune below it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
