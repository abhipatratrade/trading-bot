"""The dead-man's switch must page when it cannot read the heartbeat at all.

Regression for 2026-09-28 → 30: a SQLAlchemy 2.1 rebuild left the Railway
scheduler without its DB driver, and ``_heartbeat_watch`` logged the failure
every two minutes for two days without sending one message. A watchdog that
cannot see is an outage, not a log line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import src.core.alerts as alerts
from src.entrypoints import run_scheduler


@pytest.fixture
def harness(monkeypatch):
    """Recorded alerts, a hand-driven monotonic clock, and a switchable DB."""
    alerts.reset_alert_dedup()
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    clock = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        run_scheduler, "get_settings", lambda: SimpleNamespace(heartbeat_stale_seconds=300)
    )
    db = {"down": True}

    def _last_beat(service: str):
        if db["down"]:
            raise ModuleNotFoundError("No module named 'psycopg'")
        return datetime.now(tz=UTC)  # a fresh beat: the bot itself is fine

    monkeypatch.setattr(run_scheduler, "last_beat", _last_beat)
    yield SimpleNamespace(sent=sent, clock=clock, db=db)
    alerts.reset_alert_dedup()


def test_a_single_failed_read_does_not_page(harness) -> None:
    run_scheduler._heartbeat_watch()
    assert harness.sent == []


def test_a_sustained_blind_watch_pages_once(harness) -> None:
    for _ in range(10):  # 20 minutes of the 2-minute cadence
        run_scheduler._heartbeat_watch()
        harness.clock[0] += 120

    assert len(harness.sent) == 1
    assert "DEAD-MAN'S SWITCH IS BLIND" in harness.sent[0]


def test_recovery_pings_once_and_rearms(harness) -> None:
    for _ in range(5):
        run_scheduler._heartbeat_watch()
        harness.clock[0] += 120
    assert len(harness.sent) == 1

    harness.db["down"] = False
    run_scheduler._heartbeat_watch()
    run_scheduler._heartbeat_watch()
    assert len(harness.sent) == 2
    assert "can read the database again" in harness.sent[1]

    # The next episode is timed afresh: a single blip does not page.
    harness.db["down"] = True
    run_scheduler._heartbeat_watch()
    assert len(harness.sent) == 2


def test_a_blip_that_heals_inside_the_grace_stays_silent(harness) -> None:
    run_scheduler._heartbeat_watch()
    harness.clock[0] += 120
    harness.db["down"] = False
    run_scheduler._heartbeat_watch()
    assert harness.sent == []
