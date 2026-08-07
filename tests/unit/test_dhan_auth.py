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
    # status_code is part of the real httpx.Response contract and the manager
    # reads it to spot a 429. A fake missing it does not just fail — it fails
    # as a generic exception inside the retry loop, which is indistinguishable
    # from a network error.
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.text = json.dumps(payload)
        self.status_code = status_code

    def raise_for_status(self) -> None:  # pragma: no cover - always ok here
        pass

    def json(self) -> dict[str, object]:
        return self._payload


# Dhan's real refusal when the 2-minute mint cooldown is still running: an
# ordinary 200 whose body carries the error. Verified against the live journal
# during the 2026-08-04/05 outage.
_RATE_LIMIT_BODY: dict[str, object] = {
    "message": "Token can be generated once every 2 minutes.",
    "status": "error",
}


class _FakeHttp:
    """Records POSTs and returns a queued access token.

    An entry of ``None`` in ``tokens`` yields Dhan's rate-limit refusal instead
    of a token, so a test can reproduce the cooldown without a real endpoint.
    """

    def __init__(self, tokens: list[str | None]) -> None:
        self._tokens = list(tokens)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, params: dict[str, object] | None = None) -> _FakeResp:
        self.calls.append({"url": url, "params": params})
        tok = self._tokens.pop(0)
        if tok is None:
            return _FakeResp(_RATE_LIMIT_BODY)
        return _FakeResp({"accessToken": tok})


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
        client_id="1000000001", pin="0000", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: now[0],
    )
    assert mgr.can_refresh is True
    assert mgr.token() == fresh
    # cached — a second call within validity does not refresh again
    assert mgr.token() == fresh
    assert len(http.calls) == 1
    assert http.calls[0]["params"]["dhanClientId"] == "1000000001"
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
    # Past the mint cooldown, so a re-mint is actually possible. Without
    # advancing the clock Dhan would refuse (see the cooldown tests below) and
    # the manager correctly declines to ask.
    now[0] += _MINT_COOLDOWN_SECONDS + 1
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


# ── Refresh hardening (Decision 029): retry + last-good fallback ─────────
import time as _time_mod  # noqa: E402

import httpx  # noqa: E402

from src.brokers.dhan.auth import (  # noqa: E402
    _MAX_REJECTED_SERVES,
    _MINT_COOLDOWN_SECONDS,
)


class _FlakyHttp:
    """Mint endpoint that fails the first ``fail_first`` calls, then succeeds.

    ``fail_mode``: "no_token" (200 but empty body) or "http" (raise_for_status).
    """

    def __init__(self, tokens: list[str], fail_first: int, fail_mode: str) -> None:
        self._tokens = list(tokens)
        self._fail_first = fail_first
        self._fail_mode = fail_mode
        self.calls = 0

    def post(self, url: str, params: dict[str, object] | None = None) -> object:
        self.calls += 1
        if self.calls <= self._fail_first:
            if self._fail_mode == "no_token":
                return _FakeResp({"message": "Invalid TOTP"})
            resp = httpx.Response(429, request=httpx.Request("POST", url))

            class _R:
                text = "rate limited"

                @staticmethod
                def raise_for_status() -> None:
                    resp.raise_for_status()

                @staticmethod
                def json() -> dict[str, object]:
                    return {}

            return _R()
        return _FakeResp({"accessToken": self._tokens.pop(0)})


def _mgr(http: object, clock_val: float = 1_000_000.0) -> DhanTokenManager:
    return DhanTokenManager(
        client_id="C",
        pin="1234",
        totp_secret=_TOTP_SECRET,
        http=http,  # type: ignore[arg-type]
        clock=lambda: clock_val,
    )


def test_refresh_retries_with_fresh_totp_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    good = _fake_jwt(2_000_000)  # far-future expiry
    http = _FlakyHttp([good], fail_first=2, fail_mode="no_token")
    mgr = _mgr(http)
    assert mgr.token() == good
    assert http.calls == 3  # two Invalid-TOTP, then a mint


def test_transient_failure_falls_back_to_cached_token(monkeypatch) -> None:
    """The 2026-07-22 bug: a good token, a spurious 401, a mint on cooldown."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    good = _fake_jwt(2_000_000)
    # First call mints the good token; later re-mints all fail (cooldown).
    http = _FlakyHttp([good], fail_first=0, fail_mode="no_token")
    mgr = _mgr(http)
    assert mgr.token() == good  # initial mint

    # Simulate a spurious 401: client invalidates, then the mint is down.
    http._fail_first = 99  # every subsequent mint now fails
    mgr.invalidate()
    # Must NOT raise — the cached token is still valid, so serve it.
    assert mgr.token() == good
    # And it must not have ASKED. The mint we just made started Dhan's 2-minute
    # lockout, so every retry would be refused, and each refusal re-arms the
    # window — that feedback loop is what kept the bot locked out for two days
    # on 2026-08-04/05. Not asking is the fix, so the count stays at the
    # initial mint rather than the 1 + _REFRESH_ATTEMPTS the old code burned.
    assert http.calls == 1


# ── The 2026-08-04/05 outage: rate limit, cooldown, dead-token escape ────
def test_rate_limit_reply_is_recognised_despite_http_200(monkeypatch) -> None:
    """Dhan refuses with a 200 whose BODY carries the error.

    raise_for_status() passes it, so before this the refusal was indistinguish-
    able from any other empty response and got retried straight back into the
    lockout it was announcing.
    """
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    now = [1_000_000.0]
    http = _FakeHttp(tokens=[None, None, None])  # every mint refused
    mgr = DhanTokenManager(
        client_id="c", pin="p", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: now[0],
    )
    with pytest.raises(RuntimeError, match="no valid cached token"):
        mgr.token()
    # ONE attempt, not _REFRESH_ATTEMPTS: retrying into a rate limit extends it.
    assert len(http.calls) == 1


def test_mint_is_not_attempted_during_the_cooldown(monkeypatch) -> None:
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
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

    # Inside the cooldown: serve what we have, ask nobody.
    now[0] += _MINT_COOLDOWN_SECONDS - 1
    assert mgr.token() == first
    assert len(http.calls) == 1

    # Past it: minting resumes.
    now[0] += 2
    mgr.invalidate()
    assert mgr.token() == second
    assert len(http.calls) == 2


def test_persistently_rejected_token_eventually_raises(monkeypatch) -> None:
    """The heart of the outage: a dead token that is still timestamp-valid.

    The last-good fallback exists for a SPURIOUS 401, so a few serves are
    correct. But the token here is genuinely dead — every call 401s — and the
    old code served it forever because its own `exp` was in the future. Two
    trading days of scans fetched nothing behind exactly this loop. It must
    give up and raise, loudly, so scan_coverage can halt the bucket.
    """
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    now = [1_000_000.0]
    good = _fake_jwt(int(now[0]) + 86400)  # valid for a day by its own clock
    http = _FakeHttp(tokens=[good] + [None] * 20)  # mint once, then always refused
    mgr = DhanTokenManager(
        client_id="c", pin="p", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: now[0],
    )
    assert mgr.token() == good

    # Each round trip is a real 401 from the server on that same token.
    for _ in range(_MAX_REJECTED_SERVES):
        now[0] += _MINT_COOLDOWN_SECONDS + 1
        mgr.invalidate()
        assert mgr.token() == good  # tolerated: might be propagation lag

    now[0] += _MINT_COOLDOWN_SECONDS + 1
    mgr.invalidate()
    with pytest.raises(RuntimeError, match="being rejected by the server"):
        mgr.token()


def test_a_successful_mint_clears_the_rejection_streak(monkeypatch) -> None:
    """A recovered account must not stay one strike from raising."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    now = [1_000_000.0]
    good = _fake_jwt(int(now[0]) + 86400)
    fresh = _fake_jwt(int(now[0]) + 90000)
    http = _FakeHttp(tokens=[good, None, fresh] + [None] * 20)
    mgr = DhanTokenManager(
        client_id="c", pin="p", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: now[0],
    )
    assert mgr.token() == good

    now[0] += _MINT_COOLDOWN_SECONDS + 1
    mgr.invalidate()
    assert mgr.token() == good  # refused once → one rejected serve banked

    now[0] += _MINT_COOLDOWN_SECONDS + 1
    mgr.invalidate()
    assert mgr.token() == fresh  # mint succeeds → streak reset

    # Which means the full tolerance is available again.
    for _ in range(_MAX_REJECTED_SERVES):
        now[0] += _MINT_COOLDOWN_SECONDS + 1
        mgr.invalidate()
        assert mgr.token() == fresh


def test_no_cached_token_and_refresh_fails_raises(monkeypatch) -> None:
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    http = _FlakyHttp([], fail_first=99, fail_mode="no_token")
    mgr = _mgr(http)  # cold start, no last-good token
    with pytest.raises(RuntimeError, match="no valid cached token"):
        mgr.token()


def test_expired_cached_token_is_not_served(monkeypatch) -> None:
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    short = _fake_jwt(1_000_030)  # expires 30s after clock → inside the 60s guard
    http = _FlakyHttp([short], fail_first=0, fail_mode="no_token")
    mgr = _mgr(http, clock_val=1_000_000.0)
    assert mgr.token() == short  # mint ok

    http._fail_first = 99
    mgr.invalidate()
    # Cached token is within 60s of expiry → must refuse, not serve it stale.
    with pytest.raises(RuntimeError, match="no valid cached token"):
        mgr.token()


# ── Persistent cross-process token cache (Decision 029) ─────────────────
import os  # noqa: E402
import stat as _stat  # noqa: E402
from pathlib import Path as _Path  # noqa: E402


def _mgr_cached(
    http: object, path: _Path, clock_val: float = 1_000_000.0
) -> DhanTokenManager:
    return DhanTokenManager(
        client_id="C",
        pin="1234",
        totp_secret=_TOTP_SECRET,
        http=http,  # type: ignore[arg-type]
        clock=lambda: clock_val,
        token_cache_path=path,
    )


def test_mint_writes_cache_then_peer_reads_it_without_minting(
    monkeypatch, tmp_path: _Path
) -> None:
    """The whole point: process B reuses process A's token, no second mint."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    cache = tmp_path / "tok.json"
    good = _fake_jwt(2_000_000)

    a = _mgr_cached(_FlakyHttp([good], 0, "no_token"), cache)
    assert a.token() == good  # process A mints + writes cache
    assert cache.exists()

    # Process B: mint endpoint would FAIL, but the cache seeds a valid token.
    b_http = _FlakyHttp([], fail_first=99, fail_mode="no_token")
    b = _mgr_cached(b_http, cache)
    assert b.token() == good
    assert b_http.calls == 0, "B must not mint — it loaded the shared token"


def test_cache_file_is_owner_only(monkeypatch, tmp_path: _Path) -> None:
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    cache = tmp_path / "tok.json"
    _mgr_cached(_FlakyHttp([_fake_jwt(2_000_000)], 0, "no_token"), cache).token()
    mode = _stat.S_IMODE(cache.stat().st_mode)
    # POSIX: 0600. On Windows chmod is a near-no-op, so only assert there.
    if os.name == "posix":
        assert mode == 0o600, oct(mode)


def test_expired_cache_is_ignored_and_remint(monkeypatch, tmp_path: _Path) -> None:
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    cache = tmp_path / "tok.json"
    cache.write_text(json.dumps({"token": _fake_jwt(1_000_010), "minted_at": 0}))
    fresh = _fake_jwt(2_000_000)
    http = _FlakyHttp([fresh], fail_first=0, fail_mode="no_token")
    # Cached token expires 10s after clock → inside the 60s guard → mint anew.
    mgr = _mgr_cached(http, cache, clock_val=1_000_000.0)
    assert mgr.token() == fresh
    assert http.calls == 1


def test_corrupt_cache_is_ignored(monkeypatch, tmp_path: _Path) -> None:
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    cache = tmp_path / "tok.json"
    cache.write_text("{not valid json")
    fresh = _fake_jwt(2_000_000)
    mgr = _mgr_cached(_FlakyHttp([fresh], 0, "no_token"), cache)
    assert mgr.token() == fresh  # falls through to a mint, no crash


def test_failed_mint_picks_up_peer_token_from_disk(
    monkeypatch, tmp_path: _Path
) -> None:
    """A's token goes bad; B minted a newer one; A must adopt B's from disk."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    cache = tmp_path / "tok.json"
    old, new = _fake_jwt(2_000_000), _fake_jwt(2_000_500)

    a = _mgr_cached(_FlakyHttp([old], 0, "no_token"), cache)
    assert a.token() == old

    # A peer writes a fresher token to the shared cache.
    cache.write_text(json.dumps({"token": new, "minted_at": 1.0}))

    # A's token 401s → invalidate → re-mint fails → adopt the peer's token.
    a._http._fail_first = 99  # type: ignore[attr-defined]
    a.invalidate()
    assert a.token() == new


# ── Peer-adopt on single-session invalidation (2026-07-23 live incident) ──
import json as _json_mod  # noqa: E402


def _refreshable(cache_path, http, clock_val=1_000_000.0):
    return DhanTokenManager(
        client_id="C", pin="1234", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: clock_val, token_cache_path=cache_path,
    )


def test_rejected_token_adopts_peer_cache_without_minting(tmp_path, monkeypatch) -> None:
    """A peer minted (killing ours server-side) → adopt theirs, don't mint."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    bad = _fake_jwt(2_000_000)
    peer = _fake_jwt(2_100_000)                    # different, valid, peer-minted
    cache = tmp_path / "tok.json"
    cache.write_text(_json_mod.dumps({"token": peer, "minted_at": 0}))
    http = _FlakyHttp([], fail_first=99, fail_mode="no_token")  # any mint fails
    mgr = _refreshable(cache, http)
    mgr._token = mgr._last_good_token = bad         # our (about-to-be-rejected) token
    mgr._exp = mgr._last_good_exp = jwt_exp(bad)

    mgr.invalidate()                                # broker rejected `bad`
    assert mgr.token() == peer                      # adopted the peer token
    assert http.calls == 0, "must NOT mint when a fresh peer token exists"


def test_cache_holding_rejected_token_forces_mint(tmp_path, monkeypatch) -> None:
    """If the cache still holds the REJECTED token, don't re-adopt it — mint."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    bad = _fake_jwt(2_000_000)
    fresh = _fake_jwt(2_200_000)
    cache = tmp_path / "tok.json"
    cache.write_text(_json_mod.dumps({"token": bad, "minted_at": 0}))  # stale peer
    http = _FlakyHttp([fresh], fail_first=0, fail_mode="no_token")
    mgr = _refreshable(cache, http)
    mgr._token = mgr._last_good_token = bad
    mgr._exp = mgr._last_good_exp = jwt_exp(bad)

    mgr.invalidate()
    assert mgr.token() == fresh                     # minted a new one
    assert http.calls == 1, "cache==rejected must not be re-adopted"


# ── Cross-machine remote store (2026-07-23 shared-token fix) ─────────────
class _FakeStore:
    """In-memory TokenStore double. ``raises`` makes both methods throw, to
    prove the manager stays fail-soft when the DB is down."""

    def __init__(self, token: str | None = None, *, raises: bool = False) -> None:
        self.token = token
        self.saved: list[str] = []
        self._raises = raises

    def load(self) -> str | None:
        if self._raises:
            raise RuntimeError("db down")
        return self.token

    def save(self, token: str) -> None:
        if self._raises:
            raise RuntimeError("db down")
        self.token = token
        self.saved.append(token)


def _mgr_remote(http, store, *, cache_path=None, clock_val=1_000_000.0):
    return DhanTokenManager(
        client_id="C", pin="1234", totp_secret=_TOTP_SECRET,
        http=http, clock=lambda: clock_val,
        token_cache_path=cache_path, remote_store=store,
    )


def test_mint_publishes_to_remote_store(monkeypatch) -> None:
    """Every fresh mint is written to the shared row for other-VM peers."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    good = _fake_jwt(2_000_000)
    store = _FakeStore()
    mgr = _mgr_remote(_FlakyHttp([good], 0, "no_token"), store)
    assert mgr.token() == good
    assert store.saved == [good]  # published for the recorder's VM to read


def test_cold_start_adopts_remote_token_without_minting(monkeypatch) -> None:
    """The recorder's case: a peer minted, the row holds it → read, don't mint."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    peer = _fake_jwt(2_000_000)
    store = _FakeStore(token=peer)
    http = _FlakyHttp([], fail_first=99, fail_mode="no_token")  # any mint fails
    mgr = _mgr_remote(http, store)
    assert mgr.token() == peer
    assert http.calls == 0, "must read the shared row, not mint"


def test_load_peer_prefers_fresher_of_file_and_remote(monkeypatch, tmp_path) -> None:
    """When both peer sources hold a token, the later-expiry one wins."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    older, newer = _fake_jwt(2_000_000), _fake_jwt(2_500_000)
    cache = tmp_path / "tok.json"
    cache.write_text(_json_mod.dumps({"token": older, "minted_at": 0}))
    store = _FakeStore(token=newer)  # remote is fresher than the file
    http = _FlakyHttp([], fail_first=99, fail_mode="no_token")
    mgr = _mgr_remote(http, store, cache_path=cache)
    assert mgr.token() == newer
    assert http.calls == 0


def test_remote_store_down_falls_back_to_mint(monkeypatch) -> None:
    """A DB outage must not raise into the token path — mint as usual."""
    monkeypatch.setattr(_time_mod, "sleep", lambda _s: None)
    good = _fake_jwt(2_000_000)
    store = _FakeStore(raises=True)  # both load() and save() throw
    mgr = _mgr_remote(_FlakyHttp([good], 0, "no_token"), store)
    assert mgr.token() == good  # save() raised internally but mint still returns
    assert store.saved == []  # nothing persisted, and no exception surfaced
