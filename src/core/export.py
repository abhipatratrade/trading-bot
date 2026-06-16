"""
Nightly trade export — Parquet + CSV to a local folder, optionally synced to Google Drive.

Called by the scheduler.  Dumps all trades for the previous calendar day
(UTC) to ``data/exports/trades_YYYYMMDD.parquet`` and ``.csv``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from src.core.config import get_settings
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import Trade

_log = get_logger("core.export")
_EXPORT_DIR = Path("data/exports")


def export_trades(target_date: datetime | None = None) -> Path | None:
    """Export yesterday's trades to Parquet + CSV.  Returns the Parquet path."""
    if target_date is None:
        target_date = datetime.now(UTC) - timedelta(days=1)

    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    date_str = day_start.strftime("%Y%m%d")

    with session_scope() as session:
        trades = list(
            session.execute(
                select(Trade).where(
                    Trade.created_at >= day_start,
                    Trade.created_at < day_end,
                )
            ).scalars()
        )

    if not trades:
        _log.info("export_no_trades", date=date_str)
        return None

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

    df = pd.DataFrame(rows)
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    parquet_path = _EXPORT_DIR / f"trades_{date_str}.parquet"
    csv_path = _EXPORT_DIR / f"trades_{date_str}.csv"

    df.to_parquet(parquet_path, index=False)
    df.to_csv(csv_path, index=False)

    _log.info("export_complete", date=date_str, count=len(rows), path=str(parquet_path))
    return parquet_path


def upload_to_gdrive(local_path: Path) -> bool:
    """Upload a file to Google Drive if configured.  Returns True on success.

    ``GDRIVE_SERVICE_ACCOUNT_JSON`` accepts either form:
      - a filesystem path to a JSON key file
      - the JSON key contents inline (starts with ``{``)
    The inline form is the practical choice on Railway/containers where
    no key file is mounted.
    """
    settings = get_settings()
    if not settings.gdrive_enabled:
        _log.info("gdrive_disabled")
        return False

    try:
        import json

        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        _log.warning("gdrive_deps_missing")
        return False

    scopes = ["https://www.googleapis.com/auth/drive.file"]
    value = settings.gdrive_service_account_json or ""

    try:
        if value.lstrip().startswith("{"):
            info = json.loads(value)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=scopes
            )
        else:
            creds = service_account.Credentials.from_service_account_file(
                value, scopes=scopes
            )
        service = build("drive", "v3", credentials=creds)

        file_metadata = {
            "name": local_path.name,
            "parents": [settings.gdrive_folder_id],
        }
        media = MediaFileUpload(str(local_path), resumable=True)
        service.files().create(  # noqa: S106
            body=file_metadata, media_body=media, fields="id"
        ).execute()

        _log.info("gdrive_upload_complete", file=local_path.name)
        return True
    except Exception:
        _log.warning("gdrive_upload_failed", exc_info=True)
        return False
