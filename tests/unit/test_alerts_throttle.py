"""``send_alert_throttled`` must survive a restart, which is its whole point.

``send_alert_dedup`` counts in a module-level dict. That is right for a
long-lived loop and useless for a startup failure: systemd hands every restart
a fresh interpreter and an empty dict, so the counter never reaches its cap.

On 2026-08-30 a ``contract_spec`` TypeError crash-looped the bot 414 times and
sent **829 Telegram messages in 20 hours**. The damage was not the noise — it
was that the capped dead-man's-switch alert, the one that actually named the
fault, was buried in the flood. Hence a throttle whose memory is on disk.
"""

from __future__ import annotations

import json
import time

from src.core import alerts


def _capture(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_alert", lambda msg: (sent.append(msg), True)[1])
    return sent


def test_first_call_sends(tmp_path, monkeypatch) -> None:
    sent = _capture(monkeypatch)
    ok = alerts.send_alert_throttled(
        "k", "boom", 3600, path=tmp_path / "throttle.json"
    )
    assert ok and len(sent) == 1
    assert "boom" in sent[0]


def test_second_call_within_the_window_is_dropped(tmp_path, monkeypatch) -> None:
    sent = _capture(monkeypatch)
    p = tmp_path / "throttle.json"
    alerts.send_alert_throttled("k", "boom", 3600, path=p)
    assert alerts.send_alert_throttled("k", "boom", 3600, path=p) is False
    assert len(sent) == 1


def test_it_survives_a_process_restart(tmp_path, monkeypatch) -> None:
    """The regression that matters: in-memory state would reset here."""
    sent = _capture(monkeypatch)
    p = tmp_path / "throttle.json"
    alerts.send_alert_throttled("k", "boom", 3600, path=p)

    # Simulate the restart the way systemd does — wipe every in-process
    # counter. A disk-backed throttle must not care.
    alerts._dedup_state.clear()

    assert alerts.send_alert_throttled("k", "boom", 3600, path=p) is False
    assert len(sent) == 1, "the throttle forgot across a restart"


def test_the_window_re_arms(tmp_path, monkeypatch) -> None:
    """A bot down for hours must keep reminding, just not every 3 seconds."""
    sent = _capture(monkeypatch)
    p = tmp_path / "throttle.json"
    alerts.send_alert_throttled("k", "boom", 3600, path=p)
    p.write_text(json.dumps({"k": time.time() - 4000}))  # window elapsed
    assert alerts.send_alert_throttled("k", "boom", 3600, path=p) is True
    assert len(sent) == 2


def test_distinct_keys_do_not_throttle_each_other(tmp_path, monkeypatch) -> None:
    sent = _capture(monkeypatch)
    p = tmp_path / "throttle.json"
    alerts.send_alert_throttled("a", "first", 3600, path=p)
    alerts.send_alert_throttled("b", "second", 3600, path=p)
    assert len(sent) == 2


def test_unreadable_state_fails_open(tmp_path, monkeypatch) -> None:
    """A corrupt file must not silence alerting — that is how outages hide."""
    sent = _capture(monkeypatch)
    p = tmp_path / "throttle.json"
    p.write_text("{ this is not json")
    assert alerts.send_alert_throttled("k", "boom", 3600, path=p) is True
    assert len(sent) == 1


def test_unwritable_path_still_sends(tmp_path, monkeypatch) -> None:
    """Losing the ability to remember must not cost us the alert itself."""
    sent = _capture(monkeypatch)
    target = tmp_path / "nested" / "throttle.json"

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(alerts.Path, "write_text", _boom)
    assert alerts.send_alert_throttled("k", "boom", 3600, path=target) is True
    assert len(sent) == 1


def test_the_message_says_it_is_throttled(tmp_path, monkeypatch) -> None:
    sent = _capture(monkeypatch)
    alerts.send_alert_throttled("k", "boom", 1800, path=tmp_path / "t.json")
    assert "suppressed for 30m" in sent[0]
