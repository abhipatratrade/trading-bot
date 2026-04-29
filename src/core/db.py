"""
SQLAlchemy engine + session plumbing.

Sync-only. The bot loop, dashboard, and scheduler all use the same engine.
Reasons to stay sync:
- Simpler reasoning, easier tests.
- Postgres on Railway is co-located with the bot — connection latency is
  not a bottleneck.
- We can add an async engine later if we ever need it.

Usage:
    from src.core.db import session_scope
    with session_scope() as s:
        s.add(trade)
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models. Imported by core.models."""


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Lazy singleton engine. Reads DATABASE_URL via settings on first call."""
    global _engine
    if _engine is None:
        url = get_settings().database_url
        # pool_pre_ping handles Railway's idle-connection drops gracefully.
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager that commits on success and rolls back on error.

    Use this for any DB write. For read-only paths, prefer a plain
    ``Session()`` from the factory.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_tests() -> None:
    """Test helper: drop the cached engine so a new DATABASE_URL takes effect."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
