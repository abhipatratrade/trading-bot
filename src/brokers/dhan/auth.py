"""
Dhan access-token manager — TOTP auto-refresh.

Dhan capped directly-generated access tokens at 24h on 2025-10-01 (SEBI API
rule), so a pasted token goes stale mid-run. This mints a fresh ~24h token on
demand from the account's client id + login PIN + a live TOTP code
(``POST auth.dhan.co/app/generateAccessToken``), decoding the returned JWT's
``exp`` to refresh proactively before expiry.

Shared by the Dhan market-data adapter (``src.data_sources.dhan``) and the
broker client (``src.brokers.dhan.client``). Thread-safe: the bot's stop sweep
and tick loop can hit the same manager.

Two modes:
  * **Refreshable** — client_id + pin + totp_secret set: mints/refreshes the
    live token. Used for market data (always live) and live orders.
  * **Static** — only ``static_token`` set: returns it verbatim, never
    refreshes. Used for the DevPortal sandbox ORDER token, which the TOTP
    endpoint does not mint.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from src.core.logging import get_logger

_AUTH_BASE = "https://auth.dhan.co"
_log = get_logger("brokers.dhan.auth")

# Cross-process token cache. Dhan mints at most once per 2 minutes per account,
# and every process (bot, dry run, prepare job) builds its own manager — so
# without a SHARED cache a restart, or two processes starting close together,
# collide on the mint cooldown and cannot get a token for up to 2 minutes
# (observed 2026-07-22). A 0600 file lets them reuse one 24h token. The token
# is strictly less sensitive than the TOTP secret + PIN already on this disk.
DEFAULT_TOKEN_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "state" / "dhan_token_cache.json"
)

# Token-refresh hardening (Decision 029 — ported from scripts/dhan-scanner,
# which learned this the hard way on the 2026-07-13/14 soak).
#
# Dhan mints a fresh token from a 30-second TOTP code but blocks re-minting for
# ~2 minutes after a successful mint. So a SPURIOUS 401 on a freshly-minted
# token (server-side propagation lag) is a trap: the client invalidates the
# good token, then cannot re-mint for 2 minutes, and every fetch in that window
# fails. Observed live 2026-07-22 — a transient 401 on the first scan symbol
# dropped it and the next from the morning cut.
#
# Two defences: retry the mint a few times with a FRESH TOTP each attempt (to
# ride out a bad 30s window), and — the important one — keep the last
# successfully-minted token and fall back to it when a re-mint fails but that
# token has not actually expired.
_REFRESH_ATTEMPTS = 3
_REFRESH_RETRY_SLEEP = 2.0
# Don't serve a cached token this close to its own expiry.
_CACHED_TOKEN_MIN_REMAINING = 60


def jwt_exp(token: str) -> int | None:
    """Best-effort parse of a JWT's ``exp`` (unix seconds); None if unparseable.

    Dhan access tokens are JWTs whose payload carries ``exp``. We only read it
    to schedule refresh — signature is not verified (the server does that).
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — malformed token → just don't schedule
        return None


class DhanTokenManager:
    """Provides a valid Dhan access token, refreshing via TOTP before expiry."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        pin: str | None = None,
        totp_secret: str | None = None,
        static_token: str | None = None,
        refresh_margin_seconds: int = 1800,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        token_cache_path: Path | None = None,
    ) -> None:
        self._client_id = client_id
        self._pin = pin
        self._totp_secret = totp_secret
        self._margin = refresh_margin_seconds
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self._clock = clock
        # Persistent cross-process cache. None disables it (tests, static mode).
        self._cache_path = token_cache_path
        self._token: str | None = static_token
        self._exp: int | None = jwt_exp(static_token) if static_token else None
        # Last token we KNOW was minted successfully, retained across
        # invalidate() so a failed re-mint can fall back to it.
        self._last_good_token: str | None = static_token
        self._last_good_exp: int | None = self._exp
        self._lock = threading.Lock()
        # Seed from the shared on-disk token so a cold start reuses a valid
        # token instead of minting into the 2-minute cooldown.
        if static_token is None and self.can_refresh:
            cached = self._load_cache()
            if cached is not None:
                self._token = self._last_good_token = cached
                self._exp = self._last_good_exp = jwt_exp(cached)
                _log.info("dhan_token_loaded_from_cache", exp=self._exp)

    @property
    def can_refresh(self) -> bool:
        return bool(self._client_id and self._pin and self._totp_secret)

    def token(self) -> str:
        """Return a currently-valid access token, refreshing if needed."""
        with self._lock:
            if self._needs_refresh():
                self._refresh_locked()
            if not self._token:
                raise RuntimeError(
                    "Dhan token unavailable: no static token and TOTP refresh "
                    "not configured (need client_id + pin + totp_secret)"
                )
            return self._token

    def invalidate(self) -> None:
        """Drop the ACTIVE token so the next ``token()`` refreshes.

        Called by API clients after a 401/token-expiry response. No-op for a
        static token (nothing to refresh to). ``_last_good_token`` is
        deliberately RETAINED: a 401 is often spurious (fresh-token
        propagation lag), and if re-minting is on cooldown the fallback is the
        only way to keep serving requests.
        """
        with self._lock:
            if self.can_refresh:
                self._token = None
                self._exp = None

    # ── internals ──────────────────────────────────────────────────────
    def _needs_refresh(self) -> bool:
        if self._token is None:
            return self.can_refresh
        if not self.can_refresh:
            return False  # static token — never auto-refresh
        if self._exp is None:
            return False  # unknown expiry — rely on invalidate() after a 401
        return self._clock() >= self._exp - self._margin

    def _load_cache(self) -> str | None:
        """Read a still-valid token from the shared cache file, or None.

        Ignores a missing/corrupt/expired entry silently — the caller just
        mints instead. Validity is judged by the token's own JWT ``exp`` with
        the same near-expiry guard as the in-memory fallback.
        """
        if not self._cache_path or not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text())
            tok = data.get("token")
        except (OSError, ValueError):
            return None
        if not tok:
            return None
        exp = jwt_exp(tok)
        if exp is None or self._clock() >= exp - _CACHED_TOKEN_MIN_REMAINING:
            return None
        return tok

    def _save_cache(self, token: str) -> None:
        """Persist a freshly-minted token for other processes (0600, atomic)."""
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"token": token, "minted_at": self._clock()})
            )
            os.chmod(tmp, 0o600)  # secret — owner-only
            tmp.replace(self._cache_path)  # atomic swap
        except OSError:
            _log.warning(
                "dhan_token_cache_write_failed",
                path=str(self._cache_path),
                exc_info=True,
            )

    def _cached_token_still_valid(self) -> bool:
        """True when ``_last_good_token`` exists and is not near expiry."""
        if not self._last_good_token or self._last_good_exp is None:
            return False
        return self._clock() < self._last_good_exp - _CACHED_TOKEN_MIN_REMAINING

    def _refresh_locked(self) -> None:
        """Mint a fresh token, retrying, then falling back to the cached one.

        A fresh TOTP is generated on EVERY attempt so a bad 30-second window
        (Dhan clock skew, code rollover) can clear between tries. If all
        attempts fail but the last-good token is still valid, we serve that
        rather than raise — a spurious 401 must not strand the bot while
        re-minting is on its 2-minute cooldown.
        """
        import pyotp  # local import: only the refreshable path needs it

        totp = pyotp.TOTP(self._totp_secret)
        last_err: Exception | None = None
        for attempt in range(_REFRESH_ATTEMPTS):
            if attempt:
                time.sleep(_REFRESH_RETRY_SLEEP)  # let the TOTP window advance
            try:
                r = self._http.post(
                    f"{_AUTH_BASE}/app/generateAccessToken",
                    params={
                        "dhanClientId": self._client_id,
                        "pin": self._pin,
                        "totp": totp.now(),
                    },
                )
                r.raise_for_status()
                tok = r.json().get("accessToken")
                if not tok:
                    raise RuntimeError(
                        f"Dhan token refresh returned no accessToken: {r.text[:200]}"
                    )
            except Exception as e:  # noqa: BLE001 — any failure → retry / fall back
                last_err = e
                _log.warning(
                    "dhan_token_refresh_attempt_failed",
                    attempt=attempt + 1,
                    of=_REFRESH_ATTEMPTS,
                    error=str(e)[:160],
                )
                continue
            self._token = tok
            self._exp = jwt_exp(tok)
            self._last_good_token = tok
            self._last_good_exp = self._exp
            self._save_cache(tok)  # share with other processes on this box
            _log.info("dhan_token_refreshed", exp=self._exp, attempt=attempt + 1)
            return

        # Every mint failed (typically the 2-minute cooldown). First prefer a
        # token a PEER process may have minted since we last looked — that is
        # the whole point of the shared cache — then the in-memory last-good.
        peer = self._load_cache()
        if peer is not None and peer != self._last_good_token:
            self._token = self._last_good_token = peer
            self._exp = self._last_good_exp = jwt_exp(peer)
            _log.warning("dhan_token_refresh_used_peer_cache", exp=self._exp)
            return
        if self._cached_token_still_valid():
            self._token = self._last_good_token
            self._exp = self._last_good_exp
            _log.warning(
                "dhan_token_refresh_failed_using_cached",
                exp=self._exp,
                error=str(last_err)[:160] if last_err else None,
            )
            return
        raise RuntimeError(
            f"Dhan token refresh failed after {_REFRESH_ATTEMPTS} attempts "
            f"and no valid cached token: {last_err}"
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
