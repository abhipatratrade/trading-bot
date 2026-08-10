"""
Startup must not turn a self-healing blip into an indefinite outage.

2026-08-10: a deploy restarted the bot while Dhan's ~2-minute server-side mint
cooldown was still running. The startup probe was served a rejected token, got
DH-906, and the `except` around Dhan init disabled the WHOLE account for the
process. Both Indian buckets were the only enabled ones, so `runners` was
empty, main() returned normally — and systemd's Restart=on-failure treats exit
0 as success. The service sat inactive with NRestarts=0 until a human noticed.

Compound failure, so both links are pinned here: the probe now rides out a
healable error, and an empty runner set with buckets ENABLED exits non-zero.
"""

from __future__ import annotations

import pytest

from src.brokers.dhan.client import DhanAPIError
from src.entrypoints import run_bot


class _Probe:
    """Fails `fail_times` times with `exc`, then succeeds."""

    def __init__(self, exc: Exception, fail_times: int) -> None:
        self._exc, self._left, self.calls = exc, fail_times, 0

    def get_balances(self) -> dict:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise self._exc
        return {"available": 1}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real schedule spans Dhan's 130s cooldown — don't wait for it."""
    monkeypatch.setattr(run_bot.time, "sleep", lambda _s: None)


def test_probe_rides_out_a_token_eviction() -> None:
    """The exact 2026-08-10 failure: DH-906 on a token that self-heals."""
    p = _Probe(DhanAPIError("DH-906", "Invalid Token"), fail_times=2)
    run_bot._probe_with_retry(p, "dhan")
    assert p.calls == 3


def test_probe_rides_out_a_transient_5xx() -> None:
    p = _Probe(DhanAPIError("502", "Bad Gateway"), fail_times=1)
    run_bot._probe_with_retry(p, "dhan")
    assert p.calls == 2


def test_probe_retry_schedule_spans_the_mint_cooldown() -> None:
    """A restart landing inside Dhan's ~130s cooldown is the case this exists
    for; a schedule shorter than that would give up before it could succeed."""
    assert sum(run_bot._PROBE_BACKOFF_SECONDS) > 130.0


def test_probe_fails_fast_on_a_permanent_fault() -> None:
    """The sandbox edge 403s datacenter IPs. Waiting cannot fix that, and the
    original fail-fast behaviour is still what we want."""
    p = _Probe(DhanAPIError("403", "Forbidden"), fail_times=99)
    with pytest.raises(DhanAPIError):
        run_bot._probe_with_retry(p, "dhan")
    assert p.calls == 1  # no retries burned on a permanent fault


def test_probe_gives_up_after_the_schedule() -> None:
    """A token stuck forever must still fail the account, not hang startup."""
    p = _Probe(DhanAPIError("DH-906", "Invalid Token"), fail_times=99)
    with pytest.raises(DhanAPIError):
        run_bot._probe_with_retry(p, "dhan")
    assert p.calls == 1 + len(run_bot._PROBE_BACKOFF_SECONDS)
