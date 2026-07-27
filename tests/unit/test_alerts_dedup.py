"""Tests for the time-windowed alert dedup (``src.core.alerts``).

Covers the regression observed on 2026-06-23: a transient ``tick_error``
hit the count-based cap and then went permanently silent until the bot
restarted. The window must re-arm, and recovery must page exactly once.
"""

from __future__ import annotations

import src.core.alerts as alerts


def _capture(monkeypatch) -> list[str]:
    """Replace ``send_alert`` with a recorder; return its message list."""
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    return sent


def _fake_clock(monkeypatch) -> list[float]:
    """Drive ``alerts.time.monotonic`` from ``holder[0]`` so we can age time."""
    holder = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: holder[0])
    return holder


def test_caps_at_max_then_drops(monkeypatch) -> None:
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    _fake_clock(monkeypatch)

    for _ in range(5):
        alerts.send_alert_dedup("k", "boom", max_count=3, window_seconds=3600)

    assert len(sent) == 3
    assert "suppressed for up to 60m" in sent[-1]


def test_window_rearms_after_elapse(monkeypatch) -> None:
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    clock = _fake_clock(monkeypatch)

    for _ in range(4):
        alerts.send_alert_dedup("k", "boom", max_count=2, window_seconds=100)
    assert len(sent) == 2  # capped within the window

    clock[0] += 101  # window elapses → key re-arms
    alerts.send_alert_dedup("k", "boom", max_count=2, window_seconds=100)
    assert len(sent) == 3


def test_recovery_pages_once_and_resets(monkeypatch) -> None:
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    _fake_clock(monkeypatch)

    alerts.send_alert_dedup("k", "boom", max_count=3, window_seconds=3600)
    assert len(sent) == 1

    assert alerts.note_alert_recovery("k", "ok again") is True
    assert sent[-1] == "ok again"
    # A second recovery is a no-op — the key is already cleared.
    assert alerts.note_alert_recovery("k", "ok again") is False
    assert len(sent) == 2

    # After recovery the channel re-arms immediately.
    alerts.send_alert_dedup("k", "boom", max_count=3, window_seconds=3600)
    assert len(sent) == 3


def test_recovery_noop_when_never_fired(monkeypatch) -> None:
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    _fake_clock(monkeypatch)

    assert alerts.note_alert_recovery("never", "ok") is False
    assert sent == []


# ── sustained-failure gate (Dhan token-eviction noise suppression) ──────


def test_sustained_stays_quiet_inside_grace(monkeypatch) -> None:
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    clock = _fake_clock(monkeypatch)

    # Repeated failures within the grace window must NOT page.
    for _ in range(3):
        assert alerts.note_sustained_failure("tok", "stuck", grace_seconds=180) is False
        clock[0] += 50  # 0, 50, 100s — all under 180s
    assert sent == []


def test_sustained_pages_once_past_grace(monkeypatch) -> None:
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    clock = _fake_clock(monkeypatch)

    assert alerts.note_sustained_failure("tok", "stuck", grace_seconds=180) is False
    clock[0] += 200  # now past the grace threshold
    assert alerts.note_sustained_failure("tok", "stuck", grace_seconds=180) is True
    # Still failing → no repeat page.
    clock[0] += 200
    assert alerts.note_sustained_failure("tok", "stuck", grace_seconds=180) is False
    assert sent == ["stuck"]


def test_sustained_blip_recovers_silently(monkeypatch) -> None:
    # A failure that self-heals inside the grace window pages nothing, and
    # its recovery is silent — exactly the DH-906 < 2min case.
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    clock = _fake_clock(monkeypatch)

    alerts.note_sustained_failure("tok", "stuck", grace_seconds=180)
    clock[0] += 90  # cleared before grace elapsed
    assert alerts.note_sustained_recovery("tok", "recovered") is False
    assert sent == []


def test_sustained_recovery_pages_once_if_it_alerted(monkeypatch) -> None:
    alerts.reset_alert_dedup()
    sent = _capture(monkeypatch)
    clock = _fake_clock(monkeypatch)

    alerts.note_sustained_failure("tok", "stuck", grace_seconds=180)
    clock[0] += 200
    alerts.note_sustained_failure("tok", "stuck", grace_seconds=180)  # pages
    assert alerts.note_sustained_recovery("tok", "recovered") is True
    assert sent == ["stuck", "recovered"]

    # A fresh episode is timed from scratch (state was cleared).
    assert alerts.note_sustained_failure("tok", "stuck", grace_seconds=180) is False
