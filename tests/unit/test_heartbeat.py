"""Heartbeat staleness math (dead-man's switch) — pure, no DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core.heartbeat import staleness

NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def test_fresh_beat_is_not_stale() -> None:
    stale, age = staleness(NOW - timedelta(seconds=60), NOW, 600)
    assert not stale
    assert age == 60.0


def test_old_beat_is_stale() -> None:
    stale, age = staleness(NOW - timedelta(seconds=601), NOW, 600)
    assert stale
    assert age == 601.0


def test_exactly_threshold_is_stale() -> None:
    stale, _ = staleness(NOW - timedelta(seconds=600), NOW, 600)
    assert stale


def test_missing_beat_is_stale_with_no_age() -> None:
    stale, age = staleness(None, NOW, 600)
    assert stale
    assert age is None


def test_future_beat_is_not_stale() -> None:
    # Clock skew between VM and scheduler must not page.
    stale, age = staleness(NOW + timedelta(seconds=30), NOW, 600)
    assert not stale
    assert age == -30.0
