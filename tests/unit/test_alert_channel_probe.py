"""Tests for the alert-channel self-test (``src.core.alerts.verify_alert_channel``).

Covers the 2026-08-17..21 blind spot: the Railway scheduler's Telegram token
had been revoked, ``send_alert`` swallowed every 401 into a log line nobody
read, and the dead-man's switch stayed silent through a three-day outage. The
probe must name a revoked token — and must never leak the credential, which
sits in the request URL and so lands in most httpx exception messages.
"""

from __future__ import annotations

import httpx
import pytest

import src.core.alerts as alerts

_TOKEN = "8704653720:AA-fake-secret-not-a-real-token"


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _Settings:
    def __init__(self, *, enabled: bool = True) -> None:
        self.telegram_enabled = enabled
        self.telegram_bot_token = _Secret(_TOKEN)
        self.telegram_chat_id = "8749160122"


class _Resp:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _settings(monkeypatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(alerts, "get_settings", lambda: _Settings(enabled=enabled))


def test_unconfigured_is_not_ok(monkeypatch) -> None:
    _settings(monkeypatch, enabled=False)
    ok, detail = alerts.verify_alert_channel()
    assert ok is False
    assert "not configured" in detail


def test_revoked_token_is_named(monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(alerts.httpx, "get", lambda *a, **k: _Resp(401))
    ok, detail = alerts.verify_alert_channel()
    assert ok is False
    assert "401" in detail


def test_valid_token_reports_the_bot(monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(
        alerts.httpx,
        "get",
        lambda *a, **k: _Resp(200, {"ok": True, "result": {"username": "some_bot"}}),
    )
    ok, detail = alerts.verify_alert_channel()
    assert ok is True
    assert detail == "some_bot"


def test_network_failure_is_not_ok(monkeypatch) -> None:
    def _boom(*a, **k):
        raise httpx.ConnectError("temporary failure in name resolution")

    _settings(monkeypatch)
    monkeypatch.setattr(alerts.httpx, "get", _boom)
    ok, detail = alerts.verify_alert_channel()
    assert ok is False
    assert "unreachable" in detail


@pytest.mark.parametrize("status", [401, 403, 500])
def test_status_detail_never_leaks_the_token(monkeypatch, status: int) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(alerts.httpx, "get", lambda *a, **k: _Resp(status))
    _, detail = alerts.verify_alert_channel()
    assert _TOKEN not in detail


def test_exception_detail_never_leaks_the_token(monkeypatch) -> None:
    """httpx embeds the URL — and the URL contains the bot token."""

    def _boom(*a, **k):
        raise httpx.ConnectError(
            f"failed to connect to https://api.telegram.org/bot{_TOKEN}/getMe"
        )

    _settings(monkeypatch)
    monkeypatch.setattr(alerts.httpx, "get", _boom)
    ok, detail = alerts.verify_alert_channel()
    assert ok is False
    assert _TOKEN not in detail
    assert "AA-fake-secret" not in detail
