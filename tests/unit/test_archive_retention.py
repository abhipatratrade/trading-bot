"""The audit log is never deleted ahead of its archive.

``audit_log`` is the forensic record (House Rule #8) and retention hard-deletes
it at 180 days. Until 2026-08-03 nothing archived it first — the nightly export
covered ``trade`` alone, and the Drive mirror had never run once — so that
deletion was permanent with no copy behind it.

The guard is a watermark, and its exact meaning is the load-bearing part: not
"the last day we archived" but "every day up to here is off the box". These
tests pin that distinction, because the weaker reading silently blesses a
backlog and is indistinguishable from the stronger one on a healthy system.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.core import export, retention
from src.core.retention import BLOCKED, audit_cutoff

NOW = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
RETENTION_DAYS = 180


# ---------------------------------------------------------------------------
# audit_cutoff — pure
# ---------------------------------------------------------------------------
def test_nothing_archived_means_nothing_is_deletable() -> None:
    """The state the live system was actually in: no archive had ever run."""
    assert audit_cutoff(NOW, RETENTION_DAYS, None) is None


def test_a_lagging_archive_holds_the_deletion_back() -> None:
    """Archive BEHIND the retention horizon → the archive is the binding bound.

    The horizon here is 2026-02-04 (NOW − 180d), so a watermark must be older
    than that to bind at all. An archive merely months old is still ahead of a
    180-day rule — which is why the guard costs nothing on a healthy system and
    everything on a stalled one.
    """
    archived = date(2026, 1, 10)
    cutoff = audit_cutoff(NOW, RETENTION_DAYS, archived)
    assert cutoff == datetime(2026, 1, 11, tzinfo=UTC)
    # ...and that is EARLIER than the age bound, i.e. it deletes less, not more.
    assert cutoff < NOW - timedelta(days=RETENTION_DAYS)


def test_a_caught_up_archive_leaves_the_age_rule_in_charge() -> None:
    """Archive ahead of the horizon → retention age is the binding constraint."""
    cutoff = audit_cutoff(NOW, RETENTION_DAYS, date(2026, 8, 2))
    assert cutoff == NOW - timedelta(days=RETENTION_DAYS)


def test_the_archived_day_itself_is_deletable_through_its_end() -> None:
    """Watermark names a whole UTC day, so rows are safe up to the next midnight."""
    cutoff = audit_cutoff(NOW, 0, date(2026, 6, 1))
    assert cutoff == datetime(2026, 6, 2, tzinfo=UTC)


def test_cutoff_never_exceeds_either_bound() -> None:
    for offset in range(0, 200, 17):
        archived = (NOW - timedelta(days=offset)).date()
        cutoff = audit_cutoff(NOW, RETENTION_DAYS, archived)
        assert cutoff is not None
        assert cutoff <= NOW - timedelta(days=RETENTION_DAYS) or cutoff <= (
            datetime.combine(
                archived + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            )
        )


# ---------------------------------------------------------------------------
# prune_old_rows — the audit branch
# ---------------------------------------------------------------------------
def test_prune_reports_blocked_rather_than_deleting(monkeypatch) -> None:
    """No archive → the audit table is reported BLOCKED and left alone."""
    monkeypatch.setattr(export, "audit_archived_through", lambda: None)
    monkeypatch.setattr(retention, "audit_cutoff", lambda *a, **k: None)

    deleted: list[str] = []

    class _Session:
        def execute(self, stmt):  # noqa: ANN001
            deleted.append(str(stmt.compile()))

            class _R:
                rowcount = 0

            return _R()

    from contextlib import contextmanager

    @contextmanager
    def _scope():
        yield _Session()

    monkeypatch.setattr(retention, "session_scope", _scope)
    counts = retention.prune_old_rows()

    assert counts["audit_log"] == BLOCKED
    assert not any("audit_log" in sql for sql in deleted)
    # The snapshot tables are unaffected by the audit guard.
    assert counts["scanner_snapshot"] == 0


# ---------------------------------------------------------------------------
# The watermark only advances contiguously
# ---------------------------------------------------------------------------
@pytest.fixture
def watermark(monkeypatch):
    """In-memory stand-in for the heartbeat row."""
    state: dict[str, date | None] = {"through": None}
    monkeypatch.setattr(export, "audit_archived_through", lambda: state["through"])

    def _beat(service, extra, clock=None):  # noqa: ANN001
        state["through"] = date.fromisoformat(extra["through"])

    import src.core.heartbeat as hb

    monkeypatch.setattr(hb, "beat_with", _beat)
    return state


def test_one_good_night_does_not_bless_a_backlog(watermark) -> None:
    """THE bug this guard exists to prevent.

    A system with three months of unarchived history archives one day. If the
    watermark jumped to it, retention would immediately consider everything
    older deletable — the exact data loss the guard is for.
    """
    assert export.mark_audit_archived(date(2026, 8, 2)) is False
    assert watermark["through"] is None


def test_the_watermark_advances_one_contiguous_day(watermark) -> None:
    watermark["through"] = date(2026, 8, 1)
    assert export.mark_audit_archived(date(2026, 8, 2)) is True
    assert watermark["through"] == date(2026, 8, 2)


def test_the_watermark_never_moves_backwards(watermark) -> None:
    watermark["through"] = date(2026, 8, 2)
    assert export.mark_audit_archived(date(2026, 7, 1)) is False
    assert watermark["through"] == date(2026, 8, 2)


def test_rerunning_the_same_day_is_a_no_op(watermark) -> None:
    watermark["through"] = date(2026, 8, 2)
    assert export.mark_audit_archived(date(2026, 8, 2)) is False
    assert watermark["through"] == date(2026, 8, 2)


def test_the_backfill_may_assert_the_watermark_directly(watermark) -> None:
    """It has just archived the whole range, so it can close the gap."""
    assert export.mark_audit_archived(date(2026, 8, 2), force=True) is True
    assert watermark["through"] == date(2026, 8, 2)


# ---------------------------------------------------------------------------
# Configuration — why nothing ever reached Drive
# ---------------------------------------------------------------------------
def _settings(**overrides):
    """Settings with EVERY gdrive field pinned, so `.env` cannot leak in.

    These tests originally wrote `Settings(gdrive_folder_id=...)` and passed —
    but only because the box had no credentials. The moment real GDRIVE_OAUTH_*
    values landed in `.env` they were inherited, `gdrive_enabled` flipped True,
    and three assertions inverted. Init kwargs outrank env in pydantic-settings,
    so naming all four fields makes the case under test the whole input.
    """
    from src.core.config import Settings

    fields = {
        "gdrive_folder_id": None,
        "gdrive_oauth_client_id": None,
        "gdrive_oauth_client_secret": None,
        "gdrive_oauth_refresh_token": None,
        "gdrive_service_account_json": None,
    }
    fields.update(overrides)
    return Settings(**fields)


def test_a_folder_alone_is_not_enough_to_be_enabled() -> None:
    assert _settings(gdrive_folder_id="abc123").gdrive_enabled is False


def test_credentials_without_a_folder_are_not_enough_either() -> None:
    s = _settings(
        gdrive_oauth_client_id="cid",
        gdrive_oauth_client_secret="secret",  # noqa: S106
        gdrive_oauth_refresh_token="refresh",  # noqa: S106
    )
    assert s.gdrive_oauth_configured is True
    assert s.gdrive_enabled is False


def test_oauth_credentials_enable_the_mirror() -> None:
    s = _settings(
        gdrive_folder_id="abc123",
        gdrive_oauth_client_id="cid",
        gdrive_oauth_client_secret="secret",  # noqa: S106
        gdrive_oauth_refresh_token="refresh",  # noqa: S106
    )
    assert s.gdrive_oauth_configured is True
    assert s.gdrive_enabled is True


def test_a_partial_oauth_trio_does_not_count_as_configured() -> None:
    """Two of three is a misconfiguration, not a working mirror."""
    s = _settings(
        gdrive_folder_id="abc123",
        gdrive_oauth_client_id="cid",
        gdrive_oauth_client_secret="secret",  # noqa: S106
    )
    assert s.gdrive_oauth_configured is False
    assert s.gdrive_enabled is False


def test_a_service_account_still_enables_it_for_shared_drives() -> None:
    """Kept working — it is the right mode on Workspace, wrong on @gmail.com."""
    s = _settings(gdrive_folder_id="abc123", gdrive_service_account_json="{}")
    assert s.gdrive_enabled is True
    assert s.gdrive_oauth_configured is False


def test_upload_refuses_and_says_so_when_unconfigured(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(export, "get_settings", _settings)
    target = tmp_path / "audit_20260803.parquet"
    target.write_bytes(b"x")
    assert export.upload_to_gdrive(target) is False


# ---------------------------------------------------------------------------
# The archive itself
# ---------------------------------------------------------------------------
def test_an_empty_day_writes_no_file(monkeypatch) -> None:
    """Better no archive than an empty one a later reader mistakes for proof."""
    assert export._write_frame([], "audit_20260803") is None


def test_audit_rows_round_trip_through_parquet(monkeypatch, tmp_path) -> None:
    import pandas as pd

    monkeypatch.setattr(export, "_EXPORT_DIR", tmp_path)
    rows = [
        {
            "id": 1,
            "ts": datetime(2026, 8, 3, 9, 16, tzinfo=UTC),
            "strategy_id": "swing-indian",
            "event_type": "SCANNER_RUN",
            "message": "meanrev 1h scan",
            "payload": '{"evaluated": 94}',
        }
    ]
    path = export._write_frame(rows, "audit_20260803")
    assert path is not None
    back = pd.read_parquet(path)
    assert len(back) == 1
    assert back.iloc[0]["message"] == "meanrev 1h scan"
    assert (tmp_path / "audit_20260803.csv").exists()
