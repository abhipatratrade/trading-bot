"""Dhan TOTP token manager (Phase 3/4 — src/brokers/dhan/auth.py)."""

from __future__ import annotations

import base64
import json

import pytest

from src.brokers.dhan.auth import DhanTokenManager, jwt_exp

# A syntactically valid base32 TOTP secret (pyotp needs real base32).
_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _fake_jwt(exp: int) -> str:
    """Build a token whose middle segment base64url-decodes to {"exp": exp}."""
    def b64(d: dict[str, object]) -> str:
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64({'alg': 'HS512'})}.{b64({'exp': exp})}.sig"


class _FakeResp:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:  # pragma: no cover - always ok here
        pass

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeHttp:
    """Records POSTs and returns a queued access token."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = list(tokens)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, params: dict[str, object] | None = None) -> _FakeResp:
        self.calls.append({"url": url, "params": params})
        return _FakeResp({"accessToken": self._tokens.pop(0)})


# ── jwt_exp ─────────────────────────────────────────────────────────────
def test_jwt_exp_parses_exp() -> None:
    assert jwt_exp(_fake_jwt(1783611749)) == 1783611749


def test_jwt_exp_bad_token_returns_none() -> None:
    assert jwt_exp("not-a-jwt") is None
    assert jwt_exp("") is None


# ── static-token mode ───────────────────────────────────────────────────
def test_static_token_returned_verbatim_no_refresh() -> None:
    http = _FakeHttp(tokens=[])
    mgr = DhanTokenManager(static_token="STATIC123", http=http)
    assert mgr.token() == "STATIC123"
    assert mgr.token() == "STATIC123"
    assert http.calls == []  # never hit the refresh endpoint
    assert mgr.can_refresh is False


def test_static_token_invalidate_is_noop() -> None:
    http = _FakeHttp(tokens=[])
    mgr = DhanTokenManager(static_token="STATIC123", http=http)
    mgr.invalidate()
    assert mgr.token() == "STATIC123"  # still there, nothing to refresh to


def test_no_static_and_no_refresh_raises() -> None:
    mgr = DhanTokenManager(http=_FakeHttp(tokens=[]))
    with pytest.raises(RuntimeError, match="no static token"):
        mgr.token()


# ── refreshable mode ────────────────────────────────────────────────────
def test_refresh_mints_and_caches_token() -> None:
    now = [1_000_000.0]
    fresh = _fake_jwt(int(now[0]) + 86400)
    http = _FakeHttp(tokens=[fresh])
    mgr = DhanTokenManager(
        client_id="1103267589", pin="0000", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: now[0],
    )
    assert mgr.can_refresh is True
    assert mgr.token() == fresh
    # cached — a second call within validity does not refresh again
    assert mgr.token() == fresh
    assert len(http.calls) == 1
    assert http.calls[0]["params"]["dhanClientId"] == "1103267589"
    assert len(str(http.calls[0]["params"]["totp"])) == 6


def test_proactive_refresh_within_margin() -> None:
    now = [1_000_000.0]
    first = _fake_jwt(int(now[0]) + 3600)   # expires in 1h
    second = _fake_jwt(int(now[0]) + 90000)
    http = _FakeHttp(tokens=[first, second])
    mgr = DhanTokenManager(
        client_id="c", pin="p", totp_secret=_TOTP_SECRET,
        refresh_margin_seconds=1800, http=http, clock=lambda: now[0],
    )
    assert mgr.token() == first
    now[0] += 3600 - 1700   # inside the 1800s refresh margin
    assert mgr.token() == second
    assert len(http.calls) == 2


def test_invalidate_forces_refresh() -> None:
    now = [1_000_000.0]
    first = _fake_jwt(int(now[0]) + 86400)
    second = _fake_jwt(int(now[0]) + 86400)
    http = _FakeHttp(tokens=[first, second])
    mgr = DhanTokenManager(
        client_id="c", pin="p", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: now[0],
    )
    assert mgr.token() == first
    mgr.invalidate()
    assert mgr.token() == second
    assert len(http.calls) == 2


def test_refresh_without_exp_does_not_churn() -> None:
    """A token whose exp is unparseable must not trigger endless refresh."""
    http = _FakeHttp(tokens=["opaque-token-no-jwt"])
    mgr = DhanTokenManager(
        client_id="c", pin="p", totp_secret=_TOTP_SECRET, http=http,
    )
    assert mgr.token() == "opaque-token-no-jwt"
    assert mgr.token() == "opaque-token-no-jwt"
    assert len(http.calls) == 1  # no exp → no proactive refresh
