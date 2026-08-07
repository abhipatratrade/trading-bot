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
from typing import Protocol, runtime_checkable

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

# ── The 2026-08-04/05 outage ────────────────────────────────────────────────
# The retry above is tuned for a bad 30-SECOND TOTP window. Dhan's actual mint
# limit is 2 MINUTES, so all three attempts land inside the same lockout, and
# each refused attempt appears to re-arm it. The refusal does not even look
# like one: Dhan answers **HTTP 200** with
#   {"message":"Token can be generated once every 2 minutes.","status":"error"}
# and no accessToken, so raise_for_status() passes.
#
# What that produced live: the token was genuinely dead (401 on every data
# call), _cached_token_still_valid() saw a token that was TIMESTAMP-valid and
# served it, the next call 401'd, and round it went — 3,831 mint attempts on
# 2026-08-04 and 3,336 on 2026-08-05. Both Indian buckets scanned ZERO symbols
# for two full trading days and reported it as "nothing qualified".
#
# Three fixes, all here:
#   1. Recognise the rate-limit reply and stop retrying into it immediately —
#      further attempts only extend the lockout.
#   2. Gate mint attempts on a real cooldown, so the retry storm cannot form.
#   3. Stop serving a token the server has already REJECTED. The last-good
#      fallback exists for a SPURIOUS 401 (fresh-token propagation lag), so it
#      is still allowed a few goes — but not forever. Past that it raises, and
#      the scan_coverage invariant turns the raise into a HALT.
_MINT_COOLDOWN_SECONDS = 130.0
# How many times we may serve a token the server already rejected before
# treating the rejection as real. Covers the propagation-lag case without
# letting a genuinely dead token loop indefinitely.
_MAX_REJECTED_SERVES = 3
_RATE_LIMIT_MARKERS = ("once every 2 minutes", "can be generated once")


class DhanMintRateLimitedError(RuntimeError):
    """Dhan refused to mint because the per-account cooldown is still running.

    Distinct from a generic refresh failure because the correct response is the
    opposite of a retry: back off for minutes, and serve what we have.
    """


@runtime_checkable
class TokenStore(Protocol):
    """A cross-machine shared-token backend (e.g. a Postgres row).

    The manager's on-disk cache only reaches processes on the SAME box; a
    ``TokenStore`` extends the same single-token-shared invariant to processes
    on other machines (the depth recorder's VM). It is consulted as a peer
    source next to the file cache.

    Both methods MUST be fail-soft — ``load`` returns None and ``save``
    swallows on any error — so a backend outage degrades to the file cache +
    in-memory last-good token and never raises into the trading loop. The
    manager only touches it on a refresh or after a 401, never per-tick.
    """

    def load(self) -> str | None: ...
    def save(self, token: str) -> None: ...


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
        remote_store: TokenStore | None = None,
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
        # Cross-MACHINE shared store (Postgres row). None disables it. Peered
        # with the file cache so the bot and the recorder's VM share one token.
        self._remote_store = remote_store
        self._token: str | None = static_token
        self._exp: int | None = jwt_exp(static_token) if static_token else None
        # Last token we KNOW was minted successfully, retained across
        # invalidate() so a failed re-mint can fall back to it.
        self._last_good_token: str | None = static_token
        self._last_good_exp: int | None = self._exp
        # The exact token a broker call just had REJECTED (DH-906 / 401). Dhan
        # tokens are single-session, so when a peer process mints, ours is
        # invalidated server-side while still valid by its own timestamp. We
        # remember the rejected value so refresh never re-adopts it from the
        # (still timestamp-valid) shared cache — it would just be rejected again.
        self._rejected_token: str | None = None
        # When we last ASKED Dhan to mint (success or refusal). Every attempt
        # re-arms the 2-minute lockout, so the cooldown is measured from the
        # attempt, not from the last success.
        self._last_mint_attempt: float | None = None
        # Consecutive times we have fallen back to a token the server already
        # rejected. Reset by any successful mint or peer adoption.
        self._rejected_serves = 0
        self._lock = threading.Lock()
        # Seed from the shared token (file cache and/or remote row) so a cold
        # start reuses a valid token instead of minting into the 2-min cooldown.
        if static_token is None and self.can_refresh:
            cached = self._load_peer()
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
                # Remember the bad token so refresh won't re-adopt it from the
                # shared cache (it's timestamp-valid but server-rejected).
                if self._token is not None:
                    self._rejected_token = self._token
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

    def _load_remote(self) -> str | None:
        """Read a still-valid token from the remote store, or None.

        Same near-expiry guard as ``_load_cache``. The store is itself
        fail-soft; the extra guard here just filters a stale/near-expiry row.
        """
        if self._remote_store is None:
            return None
        try:
            tok = self._remote_store.load()
        except Exception:  # noqa: BLE001 — a peer source must never raise here
            return None
        if not tok:
            return None
        exp = jwt_exp(tok)
        if exp is None or self._clock() >= exp - _CACHED_TOKEN_MIN_REMAINING:
            return None
        return tok

    def _load_peer(self) -> str | None:
        """Freshest still-valid shared token across the file cache + remote row.

        Both are peer sources written by whichever process last minted; pick
        the one with the later ``exp`` (a proxy for "minted more recently",
        since every mint gets a fresh ~24h expiry). Either source being
        unavailable degrades silently to the other, then to a mint.
        """
        best_tok: str | None = None
        best_exp = -1
        for tok in (self._load_cache(), self._load_remote()):
            if tok is None:
                continue
            exp = jwt_exp(tok)
            if exp is not None and exp > best_exp:
                best_exp, best_tok = exp, tok
        return best_tok

    def _save_cache(self, token: str) -> None:
        """Persist a freshly-minted token for other processes.

        Writes BOTH peer sources: the on-disk cache (0600, atomic — same-box
        processes) and the remote store (other machines). Each is fail-soft.
        """
        self._save_remote(token)
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

    def _save_remote(self, token: str) -> None:
        """Publish a freshly-minted token to the remote store. Fail-soft.

        A remote outage must not fail the mint — the file cache and in-memory
        token still serve this process; only cross-machine sharing degrades.
        """
        if self._remote_store is None:
            return
        try:
            self._remote_store.save(token)
        except Exception:  # noqa: BLE001 — store save is best-effort
            _log.warning("dhan_token_remote_write_failed", exc_info=True)

    def _cached_token_still_valid(self) -> bool:
        """True when ``_last_good_token`` exists and is not near expiry."""
        if not self._last_good_token or self._last_good_exp is None:
            return False
        return self._clock() < self._last_good_exp - _CACHED_TOKEN_MIN_REMAINING

    def _mint_on_cooldown(self) -> float:
        """Seconds left on Dhan's mint lockout, or 0.0 when free to try.

        Asking during the lockout is not merely wasted — the refusal re-arms
        the window, which is how the 2026-08-04 storm sustained itself.
        """
        if self._last_mint_attempt is None:
            return 0.0
        elapsed = self._clock() - self._last_mint_attempt
        return max(0.0, _MINT_COOLDOWN_SECONDS - elapsed)

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        """Is this Dhan's 'once every 2 minutes' refusal?

        Checked on a 200: the refusal carries an ordinary success status code
        and signals the error only in the body.
        """
        if response.status_code == 429:
            return True
        body = response.text[:400].lower()
        return any(marker in body for marker in _RATE_LIMIT_MARKERS)

    def _refresh_locked(self) -> None:
        """Mint a fresh token, retrying, then falling back to the cached one.

        A fresh TOTP is generated on EVERY attempt so a bad 30-second window
        (Dhan clock skew, code rollover) can clear between tries. If all
        attempts fail but the last-good token is still valid, we serve that
        rather than raise — a spurious 401 must not strand the bot while
        re-minting is on its 2-minute cooldown.
        """
        import pyotp  # local import: only the refreshable path needs it

        # Peer-first: a concurrent process may already have minted a fresh
        # token (that mint is WHY ours was rejected — single-session). Adopt
        # theirs instead of minting a competing one, or N processes thrash,
        # each mint invalidating the others every tick. Skip a cached token
        # equal to the one just rejected — it would only be rejected again.
        peer = self._load_peer()
        if peer is not None and peer != self._rejected_token:
            self._token = self._last_good_token = peer
            self._exp = self._last_good_exp = jwt_exp(peer)
            self._rejected_token = None
            self._rejected_serves = 0
            _log.info("dhan_token_adopted_peer_before_mint", exp=self._exp)
            return

        totp = pyotp.TOTP(self._totp_secret)
        last_err: Exception | None = None

        # Don't even ask while the lockout is running — the refusal would only
        # extend it. Fall through to the cached/peer token below.
        cooldown = self._mint_on_cooldown()
        if cooldown > 0:
            _log.info("dhan_token_mint_skipped_cooldown", seconds_left=int(cooldown))
            last_err = DhanMintRateLimitedError(
                f"mint cooldown: {int(cooldown)}s left before Dhan will mint again"
            )
            return self._serve_fallback_locked(last_err)

        for attempt in range(_REFRESH_ATTEMPTS):
            if attempt:
                time.sleep(_REFRESH_RETRY_SLEEP)  # let the TOTP window advance
            try:
                self._last_mint_attempt = self._clock()
                r = self._http.post(
                    f"{_AUTH_BASE}/app/generateAccessToken",
                    # These stay in the QUERY STRING: it is the only form Dhan
                    # is known to accept (see scripts/dhan-scanner), and moving
                    # them to a body could not be verified without minting a
                    # real token, which would evict the live one (single
                    # session). The logging leak they caused is fixed where it
                    # belongs — the redaction filter in src/core/logging.py now
                    # scrubs pin=/totp= by NAME.
                    params={
                        "dhanClientId": self._client_id,
                        "pin": self._pin,
                        "totp": totp.now(),
                    },
                )
                # The rate-limit refusal arrives as a 200 with an error body,
                # so it must be caught BEFORE the status check and must not be
                # retried — see the outage note at the top of this module.
                if self._is_rate_limited(r):
                    raise DhanMintRateLimitedError(
                        f"Dhan mint refused (cooldown): {r.text[:160]}"
                    )
                r.raise_for_status()
                tok = r.json().get("accessToken")
                if not tok:
                    raise RuntimeError(
                        f"Dhan token refresh returned no accessToken: {r.text[:200]}"
                    )
            except DhanMintRateLimitedError as e:
                # Retrying inside the lockout is what sustained the outage.
                last_err = e
                _log.warning(
                    "dhan_token_mint_rate_limited",
                    attempt=attempt + 1,
                    cooldown_seconds=int(_MINT_COOLDOWN_SECONDS),
                )
                break
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
            self._rejected_token = None  # a fresh mint supersedes the bad one
            self._rejected_serves = 0
            self._save_cache(tok)  # share with other processes on this box
            _log.info("dhan_token_refreshed", exp=self._exp, attempt=attempt + 1)
            return

        self._serve_fallback_locked(last_err)

    def _serve_fallback_locked(self, last_err: Exception | None) -> None:
        """Minting is unavailable — serve the best token we already hold.

        First prefer a token a PEER process may have minted since we last
        looked (the whole point of the shared cache), then the in-memory
        last-good. Raises when neither is usable.
        """
        peer = self._load_peer()
        if (
            peer is not None
            and peer != self._last_good_token
            and peer != self._rejected_token
        ):
            self._token = self._last_good_token = peer
            self._exp = self._last_good_exp = jwt_exp(peer)
            self._rejected_token = None
            self._rejected_serves = 0
            _log.warning("dhan_token_refresh_used_peer_cache", exp=self._exp)
            return

        if self._cached_token_still_valid():
            # The token we are about to serve may be the very one the server
            # just rejected. That is legitimate ONCE or twice — a 401 on a
            # freshly-minted token is usually propagation lag — but a token
            # that keeps being rejected is dead, whatever its own `exp` says.
            # Serving it regardless is precisely what made 2026-08-04/05 a
            # silent two-day outage instead of a loud alert.
            if self._last_good_token == self._rejected_token:
                self._rejected_serves += 1
                if self._rejected_serves > _MAX_REJECTED_SERVES:
                    _log.error(
                        "dhan_token_rejected_repeatedly",
                        serves=self._rejected_serves,
                        exp=self._last_good_exp,
                    )
                    raise RuntimeError(
                        "Dhan token is being rejected by the server and cannot "
                        f"be re-minted ({_MAX_REJECTED_SERVES} fallbacks "
                        f"exhausted). Last mint error: {last_err}"
                    )
                _log.warning(
                    "dhan_token_serving_rejected_token",
                    serves=self._rejected_serves,
                    of=_MAX_REJECTED_SERVES,
                )
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
