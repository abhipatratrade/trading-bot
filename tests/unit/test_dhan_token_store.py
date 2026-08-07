"""PostgresTokenStore — the cross-VM shared-token backend (2026-07-23).

These exercise the fail-soft contract without a live Postgres: the store must
NEVER raise into the token path, so a DB outage degrades to the file cache.
The DB seams (``get_session_factory`` / ``session_scope``) are monkeypatched.
"""

from __future__ import annotations

from contextlib import contextmanager

import src.brokers.dhan.token_store as ts
from src.brokers.dhan.token_store import PostgresTokenStore


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal Session double: records executes, returns a queued result."""

    def __init__(self, load_value=None):
        self._load_value = load_value
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        self.executed.append(stmt)
        return _Result(self._load_value)


# ── load ────────────────────────────────────────────────────────────────
def test_load_returns_row_token(monkeypatch) -> None:
    session = _FakeSession(load_value="TOKEN-XYZ")
    monkeypatch.setattr(ts, "get_session_factory", lambda: (lambda: session))
    store = PostgresTokenStore("1000000001", minted_by="bot")
    assert store.load() == "TOKEN-XYZ"


def test_load_returns_none_on_db_error(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ts, "get_session_factory", _boom)
    store = PostgresTokenStore("C", minted_by="recorder")
    assert store.load() is None  # fail-soft, no raise


# ── save ────────────────────────────────────────────────────────────────
def test_save_executes_upsert(monkeypatch) -> None:
    session = _FakeSession()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(ts, "session_scope", _scope)
    store = PostgresTokenStore("C", minted_by="bot")
    store.save("NEW-TOKEN")
    assert len(session.executed) == 1  # one upsert issued


def test_save_swallows_db_error(monkeypatch) -> None:
    @contextmanager
    def _scope():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(ts, "session_scope", _scope)
    store = PostgresTokenStore("C", minted_by="bot")
    store.save("NEW-TOKEN")  # must not raise
