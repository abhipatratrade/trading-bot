"""Archive dead-man's switch — the watermark stops moving, someone gets paged.

``_nightly_export`` already alerts on a failed upload, but only if the job runs
at all: a dead Railway scheduler stops the archive and every alarm about it in
one stroke. So the watcher runs on the VM instead, mirroring the Railway-side
heartbeat watch that exists because a dead VM must not silence its own
watchdog (Decision 020/033).

One symptom covers all three failure modes — dead scheduler, expired Drive
token, upload failing nightly — because each one shows up as a watermark that
stops advancing.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.export import archive_lag_days

TODAY = date(2026, 8, 4)


def test_never_archived_is_distinguishable_from_stalled() -> None:
    """None is its own alarm: "never set up" reads differently from "stopped"."""
    assert archive_lag_days(None, TODAY) is None


def test_the_healthy_state_is_one_day_behind() -> None:
    """The job archives YESTERDAY, so lag 1 is correct, not late."""
    assert archive_lag_days(date(2026, 8, 3), TODAY) == 1


def test_one_missed_night_is_tolerated_by_the_default() -> None:
    from src.core.config import Settings

    lag = archive_lag_days(date(2026, 8, 2), TODAY)
    assert lag == 2
    assert lag <= Settings(archive_stale_days=2).archive_stale_days


def test_two_missed_nights_breaches_the_default() -> None:
    from src.core.config import Settings

    lag = archive_lag_days(date(2026, 8, 1), TODAY)
    assert lag == 3
    assert lag > Settings(archive_stale_days=2).archive_stale_days


def test_a_long_stall_reports_its_true_size() -> None:
    """The message quotes the lag, so it must be the real number of days."""
    assert archive_lag_days(date(2026, 5, 1), TODAY) == 95


def test_a_watermark_at_today_is_not_stale() -> None:
    """A manual backfill can put it at today; that must not read as negative."""
    assert archive_lag_days(TODAY, TODAY) == 0


@pytest.mark.parametrize("days", [0, 1, 2, 3, 30])
def test_lag_is_monotonic_in_the_watermark(days: int) -> None:
    from datetime import timedelta

    assert archive_lag_days(TODAY - timedelta(days=days), TODAY) == days


def test_the_bot_checks_the_archive_hourly_not_every_tick() -> None:
    """A per-tick DB read for a value that moves once a day is pure noise."""
    from src.entrypoints import run_bot

    assert run_bot._ARCHIVE_CHECK_EVERY_TICKS == 60


def test_the_check_is_wired_into_the_tick_loop() -> None:
    """Guards against the helper existing but never being called."""
    import inspect

    from src.entrypoints import run_bot

    source = inspect.getsource(run_bot)
    assert "_check_archive()" in source
    assert "_ARCHIVE_CHECK_EVERY_TICKS" in source


def test_the_watcher_lives_on_the_bot_not_the_scheduler() -> None:
    """THE architectural point. If this ever moves to run_scheduler, a dead
    Railway container would stop the archive and its alarm together."""
    import inspect

    from src.entrypoints import run_bot, run_scheduler

    assert "_check_archive" in inspect.getsource(run_bot)
    assert "_check_archive" not in inspect.getsource(run_scheduler)
