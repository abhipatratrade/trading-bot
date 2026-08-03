"""
Nightly archive — Parquet + CSV to a local folder, mirrored to Google Drive.

Two tables are archived, for different reasons:

* ``trade`` — the money record. Never pruned from Postgres (House Rule #3), so
  this export is a convenience copy and a backtest input.
* ``audit_log`` — the forensic record (House Rule #8), and the ONLY reason this
  module now matters. Retention hard-deletes audit rows after 180 days, and
  until 2026-08-03 nothing archived them first: the deletion was permanent and
  had no copy behind it. ``core/retention.py`` will not prune past the
  watermark this module writes, so a failing archive stops the deletion rather
  than silently outrunning it.

WHY THE MIRROR NEVER WORKED. Three independent faults, each sufficient on its
own, all silent:

1. No credentials were ever set, so ``gdrive_enabled`` was False and the upload
   returned before trying. It logged at INFO.
2. The three ``google-*`` packages were in ``pyproject.toml`` but NOT in
   ``requirements.txt``, which is what ``ops/deploy.sh`` installs on the VM. So
   even with credentials the import would have failed.
3. Service-account auth cannot work on a personal @gmail.com at all: a service
   account has no Drive storage quota, and a file it creates in a shared My
   Drive folder would be owned by it, so Drive rejects the upload with
   ``storageQuotaExceeded``. Only a Workspace Shared Drive can hold
   service-account-owned files.

Hence :func:`upload_to_gdrive` now supports OAuth user credentials as well —
files are owned by the user and count against their own quota — and prefers
them when both modes are configured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import date as date_
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from src.core.config import get_settings
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import AuditLog, Trade

_log = get_logger("core.export")
_EXPORT_DIR = Path("data/exports")

# Watermark: the last UTC day whose audit rows are known to be OFF this box.
# Stored in ``heartbeat`` because that table already means exactly "when did
# this named job last succeed", and a one-row watermark does not earn a table
# and a migration of its own. ``_heartbeat_watch`` only watches the bot-worker,
# so an extra row here pages nobody.
ARCHIVE_AUDIT_SERVICE = "archive:audit_log"

# Drive's own scope for "files this app created". Deliberately not
# ``drive.readonly`` or full ``drive``: the mirror never needs to see, let
# alone touch, anything else in the user's Drive.
GDRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.file",)


# ---------------------------------------------------------------------------
# Local write
# ---------------------------------------------------------------------------
def _write_frame(rows: list[dict], stem: str) -> Path | None:
    """Write ``rows`` to ``data/exports/<stem>.parquet`` + ``.csv``. PURE-ish.

    Returns the Parquet path, or None when there is nothing to write — an
    empty day is not a failure, and an empty file would only invite a later
    reader to mistake "no rows" for "no archive".
    """
    if not rows:
        return None
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    parquet_path = _EXPORT_DIR / f"{stem}.parquet"
    frame.to_parquet(parquet_path, index=False)
    frame.to_csv(_EXPORT_DIR / f"{stem}.csv", index=False)
    return parquet_path


def _utc_day_bounds(target: datetime) -> tuple[datetime, datetime, str]:
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1), start.strftime("%Y%m%d")


def export_trades(target_date: datetime | None = None) -> Path | None:
    """Export one UTC day of trades. Returns the Parquet path, or None."""
    if target_date is None:
        target_date = datetime.now(UTC) - timedelta(days=1)
    day_start, day_end, date_str = _utc_day_bounds(target_date)

    with session_scope() as session:
        trades = list(
            session.execute(
                select(Trade).where(
                    Trade.created_at >= day_start,
                    Trade.created_at < day_end,
                )
            ).scalars()
        )

    rows = [
        {
            "id": t.id,
            "strategy_id": t.strategy_id,
            "broker": t.broker.value,
            "symbol": t.symbol,
            "side": t.side.value,
            "quantity": float(t.quantity),
            "price": float(t.price) if t.price else None,
            "fees": float(t.fees),
            "leverage": float(t.leverage) if t.leverage else None,
            "client_order_id": t.client_order_id,
            "exchange_order_id": t.exchange_order_id,
            "status": t.status.value,
            "submitted_at": t.submitted_at,
            "filled_at": t.filled_at,
            "created_at": t.created_at,
        }
        for t in trades
    ]
    path = _write_frame(rows, f"trades_{date_str}")
    if path is None:
        _log.info("export_no_trades", date=date_str)
    else:
        _log.info("export_complete", date=date_str, count=len(rows), path=str(path))
    return path


def export_audit_log(target_date: datetime | None = None) -> Path | None:
    """Export one UTC day of ``audit_log``. Returns the Parquet path, or None.

    ``payload`` is serialised as a JSON string rather than exploded into
    columns: it is heterogeneous by design (a scanner histogram and a breaker
    trip share no fields), and a flattened schema would differ from day to day.
    """
    if target_date is None:
        target_date = datetime.now(UTC) - timedelta(days=1)
    day_start, day_end, date_str = _utc_day_bounds(target_date)

    with session_scope() as session:
        events = list(
            session.execute(
                select(AuditLog)
                .where(AuditLog.ts >= day_start, AuditLog.ts < day_end)
                .order_by(AuditLog.ts)
            ).scalars()
        )

    import json

    rows = [
        {
            "id": e.id,
            "ts": e.ts,
            "strategy_id": e.strategy_id,
            "event_type": e.event_type.value,
            "message": e.message,
            "payload": json.dumps(e.payload, sort_keys=True, default=str)
            if e.payload is not None
            else None,
        }
        for e in events
    ]
    path = _write_frame(rows, f"audit_{date_str}")
    if path is None:
        _log.info("export_no_audit_rows", date=date_str)
    else:
        _log.info(
            "export_audit_complete", date=date_str, count=len(rows), path=str(path)
        )
    return path


# ---------------------------------------------------------------------------
# Archive watermark — what retention is allowed to delete
# ---------------------------------------------------------------------------
def mark_audit_archived(day: date_, *, force: bool = False) -> bool:
    """Advance the watermark to ``day``. Returns whether it moved. Never raises.

    The watermark means something stronger than "the last day we archived": it
    means EVERY audit day up to and including it is off the box. Retention
    deletes below it, so a weaker reading would be a data-loss bug — one good
    night would otherwise bless three months of never-archived backlog.

    So it only advances CONTIGUOUSLY, from ``day - 1``. A first run on a
    system with history therefore does not move it at all, and the audit prune
    stays blocked until ``scripts/archive_backfill.py`` has closed the gap.
    Blocked-and-growing is the right failure: recoverable, and loud.

    ``force`` is for that backfill, which has just archived the range itself
    and is the one caller entitled to assert the invariant directly.
    """
    from src.core.heartbeat import beat_with

    current = audit_archived_through()
    contiguous = current is not None and current + timedelta(days=1) == day
    if not (force or contiguous or current == day):
        _log.warning(
            "archive_watermark_gap",
            watermark=str(current) if current else None,
            archived_day=str(day),
            hint="run scripts/archive_backfill.py — the prune stays blocked",
        )
        return False
    if current is not None and current >= day:
        return False  # already at or past this day; never move backwards
    beat_with(ARCHIVE_AUDIT_SERVICE, extra={"through": day.isoformat()})
    _log.info("archive_watermark_advanced", through=day.isoformat())
    return True


def audit_archived_through() -> date_ | None:
    """The last UTC day whose audit rows reached Drive, or None if never.

    None is the honest answer for "nothing has ever been archived", and
    retention treats it as "delete nothing" rather than "delete everything".
    """
    from src.core.heartbeat import last_extra

    extra = last_extra(ARCHIVE_AUDIT_SERVICE) or {}
    raw = extra.get("through")
    if not raw:
        return None
    try:
        return date_.fromisoformat(str(raw))
    except ValueError:
        _log.warning("archive_watermark_unparseable", value=str(raw))
        return None


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
def _build_credentials():
    """OAuth user credentials if configured, else the service account.

    OAuth wins the tie deliberately: it is the mode that works on a personal
    account, so a box carrying both is almost certainly mid-migration off the
    service account rather than intending to use it.
    """
    import json

    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials

    settings = get_settings()
    if settings.gdrive_oauth_configured:
        return Credentials(
            token=None,
            refresh_token=settings.gdrive_oauth_refresh_token.get_secret_value(),  # type: ignore[union-attr]
            client_id=settings.gdrive_oauth_client_id,
            client_secret=settings.gdrive_oauth_client_secret.get_secret_value(),  # type: ignore[union-attr]
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
            scopes=list(GDRIVE_SCOPES),
        )

    value = settings.gdrive_service_account_json or ""
    if value.lstrip().startswith("{"):
        return service_account.Credentials.from_service_account_info(
            json.loads(value), scopes=list(GDRIVE_SCOPES)
        )
    return service_account.Credentials.from_service_account_file(
        value, scopes=list(GDRIVE_SCOPES)
    )


def upload_to_gdrive(local_path: Path) -> bool:
    """Mirror one file to the configured Drive folder. Returns True on success.

    Replaces a same-named file in the folder rather than adding a second copy,
    so a re-run of a night's export does not accumulate duplicates.
    """
    settings = get_settings()
    if not settings.gdrive_enabled:
        _log.warning(
            "gdrive_not_configured",
            hint="set GDRIVE_FOLDER_ID + the GDRIVE_OAUTH_* trio; "
            "see docs/runbook.md",
        )
        return False

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        # Was the second silent fault: these live in pyproject.toml, and the VM
        # installs from requirements.txt, which did not list them.
        _log.error("gdrive_deps_missing", hint="pip install -r requirements.txt")
        return False

    try:
        service = build("drive", "v3", credentials=_build_credentials())
        media = MediaFileUpload(str(local_path), resumable=True)
        existing = (
            service.files()
            .list(
                q=(
                    f"name = '{local_path.name}' and "
                    f"'{settings.gdrive_folder_id}' in parents and trashed = false"
                ),
                fields="files(id)",
                pageSize=1,
            )
            .execute()
            .get("files", [])
        )
        if existing:
            service.files().update(
                fileId=existing[0]["id"], media_body=media, fields="id"
            ).execute()
        else:
            service.files().create(
                body={
                    "name": local_path.name,
                    "parents": [settings.gdrive_folder_id],
                },
                media_body=media,
                fields="id",
            ).execute()
        _log.info("gdrive_upload_complete", file=local_path.name)
        return True
    except Exception as exc:
        # The service-account quota failure is worth naming: it looks like a
        # permissions problem and is not one, and it is the single most likely
        # error on a personal @gmail.com.
        hint = (
            "service accounts have no Drive storage quota — use the "
            "GDRIVE_OAUTH_* mode on a personal account"
            if "storageQuota" in str(exc)
            else None
        )
        _log.error(
            "gdrive_upload_failed", file=local_path.name, hint=hint, exc_info=True
        )
        return False
