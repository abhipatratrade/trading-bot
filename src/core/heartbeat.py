"""
Heartbeat / dead-man's switch (Phase 1c).

The bot-worker upserts one ``heartbeat`` row per tick from the GCP VM.
The Railway scheduler — independent infrastructure — checks the row's age
every couple of minutes and pages via Telegram when it goes stale, so a
dead VM/bot no longer fails silently. (Positions stay bounded meanwhile:
exchange-resident stops, Decision 022.)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import Heartbeat

_log = get_logger("core.heartbeat")

SERVICE_BOT_WORKER = "bot-worker"


def beat(service: str, clock: Clock | None = None) -> None:
    """Upsert the service's heartbeat row to now. Never raises."""
    now = (clock or RealClock()).now()
    try:
        with session_scope() as session:
            row = session.execute(
                select(Heartbeat).where(Heartbeat.service == service)
            ).scalar_one_or_none()
            if row is None:
                session.add(Heartbeat(service=service, beat_at=now))
            else:
                row.beat_at = now
    except IntegrityError:
        # Unique-constraint race on first beat — the other writer won.
        pass
    except Exception:
        _log.warning("heartbeat_write_failed", service=service, exc_info=True)


def last_beat(service: str) -> datetime | None:
    """The service's last heartbeat timestamp, or None if it never beat."""
    with session_scope() as session:
        row = session.execute(
            select(Heartbeat).where(Heartbeat.service == service)
        ).scalar_one_or_none()
        return row.beat_at if row is not None else None


def staleness(
    beat_at: datetime | None,
    now: datetime,
    stale_after_seconds: int,
) -> tuple[bool, float | None]:
    """Pure staleness check → (is_stale, age_seconds).

    A missing heartbeat (``beat_at`` is None) counts as stale with age
    None — a service that never registered is as dead as one that
    stopped.
    """
    if beat_at is None:
        return True, None
    age = (now - beat_at).total_seconds()
    return age >= stale_after_seconds, age
