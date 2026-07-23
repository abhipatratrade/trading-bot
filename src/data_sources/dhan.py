"""
Dhan (DhanHQ) market data — REST, equities (Decision 012; Phase 3/4).

Implements the ``MarketData`` interface against the Dhan v2 charts + quote
APIs. Market data ALWAYS uses the live ``api.dhan.co`` host (the sandbox has
no data feed); the access token is TOTP-auto-refreshed by ``DhanTokenManager``.

Symbols are human tickers (e.g. ``"SWIGGY"``); Dhan needs a numeric
``securityId`` + ``exchangeSegment``, resolved from the public scrip master
(NSE ``EQ`` + BSE equities, ISIN-deduped preferring NSE — ~4,600 names),
cached to disk and refreshed monthly.

Companion to ``src.brokers.dhan`` (orders). Funding rates are N/A for cash
equity — ``get_funding_rate`` raises.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from src.brokers.dhan.auth import DEFAULT_TOKEN_CACHE_PATH, DhanTokenManager
from src.brokers.dhan.token_store import PostgresTokenStore
from src.core.config import Settings, get_settings
from src.core.logging import get_logger
from src.data_sources.base import FundingRate, MarketData, OHLCVBar, Ticker

_log = get_logger("data_sources.dhan")

# Public scrip master (self-contained — no Backtesting Engine runtime dep).
_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
_UNIVERSE_STALE_DAYS = 30
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_DEFAULT_UNIVERSE_CACHE = _DATA_DIR / "dhan_universe.json"

# BSE cash-equity series kept when a name is BSE-only (no NSE listing).
_BSE_EQUITY_SERIES = {"A", "B", "X", "XT", "T"}

# MarketData interval → Dhan intraday interval (minutes, as string). "1d" is
# served by the daily historical endpoint instead.
_INTRADAY_MINUTES: dict[str, str] = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
}

# Charts-endpoint 429 backoff (Dhan Data API cap: 5 req/s). Attempts include
# the first try; base × 2^attempt, capped, unless the response carries a
# Retry-After header (honoured verbatim).
_CHARTS_MAX_ATTEMPTS = 5
_CHARTS_BACKOFF_BASE = 0.5
_CHARTS_BACKOFF_MAX = 8.0

# Per-request pace for the LIVE bot's Dhan client so the 99-symbol intraday
# scan stays under 5 req/s (0.22s ≈ 4.5/s, leaving headroom for jitter). NOT
# applied to the prepare job, which builds its own client and mostly uses the
# bulk-quote endpoint.
BOT_REQUEST_DELAY_SECONDS = 0.22


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds form) into seconds."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        return None
    return secs if secs >= 0 else None


class DhanData(MarketData):
    """Synchronous REST client for Dhan equity market data."""

    def __init__(
        self,
        *,
        token_manager: DhanTokenManager,
        data_base_url: str = "https://api.dhan.co",
        universe: dict[str, dict[str, str]] | None = None,
        http: httpx.Client | None = None,
        universe_cache_path: Path | None = None,
        request_delay_seconds: float = 0.0,
    ) -> None:
        self._token = token_manager
        self._base = data_base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self._universe = universe  # None ⇒ lazy-load on first resolve
        self._universe_cache = universe_cache_path or _DEFAULT_UNIVERSE_CACHE
        self._req_delay = request_delay_seconds

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        request_delay_seconds: float = 0.0,
    ) -> DhanData:
        """Build a DhanData from settings.

        ``request_delay_seconds`` paces every charts call. The live bot passes
        a small value so the intraday scan stays under Dhan's 5 req/s cap
        (Decision 029); the prepare job leaves it at 0 because it fetches via
        the bulk-quote endpoint, not per-symbol charts.
        """
        s = settings or get_settings()
        acct = s.dhan_account()
        # Cross-MACHINE peer: publish every minted token to a Postgres row so a
        # process on ANOTHER VM (the depth-data recorder) reads it instead of
        # minting a competitor — Dhan is single-session, so a competing mint
        # would evict us (2026-07-23 ping-pong). The bot is the routine minter,
        # hence minted_by="bot". Fail-soft: a DB outage degrades to the on-disk
        # cache below. Only attach when we actually have a client id to key on.
        remote_store = (
            PostgresTokenStore(acct.data_client_id, minted_by="bot")
            if acct.data_client_id
            else None
        )
        token = DhanTokenManager(
            client_id=acct.data_client_id,
            pin=acct.pin,
            totp_secret=acct.totp_secret,
            static_token=acct.data_token,
            # Shared on-disk cache so the bot, dry run, and prepare job reuse
            # one 24h token instead of each minting into Dhan's 2-min cooldown.
            token_cache_path=DEFAULT_TOKEN_CACHE_PATH,
            remote_store=remote_store,
        )
        return cls(
            token_manager=token,
            data_base_url=acct.data_base_url,
            request_delay_seconds=request_delay_seconds,
        )

    @property
    def token_manager(self) -> DhanTokenManager:
        """The shared token manager — reused by the Dhan broker in live mode
        (live orders ride the refreshed data token)."""
        return self._token

    # ── universe resolution ─────────────────────────────────────────────
    @property
    def universe(self) -> dict[str, dict[str, str]]:
        if self._universe is None:
            self._universe = self._load_universe()
        return self._universe

    def symbols(self) -> list[str]:
        """All tradeable NSE+BSE equity tickers in the resolved universe."""
        return list(self.universe.keys())

    def resolve(self, symbol: str) -> tuple[str, str]:
        """Public ticker → ``(security_id, exchange_segment)`` resolver.

        Shared with the Dhan broker client (``DhanClient`` takes this as its
        ``resolve_symbol`` callable) so both sides agree on the universe.
        """
        info = self.universe.get(symbol)
        if info is None:
            raise ValueError(f"Unknown Dhan symbol: {symbol!r}")
        return info["security_id"], info["exchange"]

    # Back-compat internal alias (data adapter's own callers).
    _resolve = resolve

    def _load_universe(self, force: bool = False) -> dict[str, dict[str, str]]:
        cache = self._universe_cache
        if cache.exists() and not force:
            age_days = (time.time() - cache.stat().st_mtime) / 86400
            if age_days < _UNIVERSE_STALE_DAYS:
                try:
                    return json.loads(cache.read_text())
                except (OSError, ValueError):
                    _log.warning("dhan_universe_cache_unreadable", path=str(cache))
        universe = self._fetch_universe()
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(universe))
        except OSError:
            _log.warning("dhan_universe_cache_write_failed", path=str(cache))
        return universe

    def _fetch_universe(self) -> dict[str, dict[str, str]]:
        """Download + parse the scrip master into ``symbol -> {id, exchange}``."""
        import pandas as pd

        _log.info("dhan_universe_fetch_start", url=_SCRIP_MASTER_URL)
        resp = self._http.get(_SCRIP_MASTER_URL, timeout=60.0)
        resp.raise_for_status()
        from io import StringIO

        m = pd.read_csv(StringIO(resp.text), dtype=str, low_memory=False)

        # Underlyings that have NSE derivatives. Their SM_UPPER/LOWER band is a
        # *dynamic* (flexible) band, not a hard circuit — it widens on a cool-off
        # rather than freezing the scrip. Non-F&O names have HARD circuits, which
        # is what can trap an intraday position (Decision 029). Captured here so
        # the intraday scanner can tell the two apart; measured 2026-07-21:
        # 97/99 NIFTY-100 are F&O, vs 18/100 of NIFTY Smallcap 100.
        fno = set(
            m[(m["SEGMENT"] == "D") & (m["EXCH_ID"] == "NSE")]["UNDERLYING_SYMBOL"]
            .dropna()
            .str.strip()
        )

        eq = m[m["SEGMENT"] == "E"]
        nse = eq[(eq["EXCH_ID"] == "NSE") & (eq["SERIES"] == "EQ")
                 & (eq["INSTRUMENT"] == "EQUITY")]
        bse = eq[(eq["EXCH_ID"] == "BSE") & (eq["INSTRUMENT"] == "EQUITY")
                 & (eq["SERIES"].isin(_BSE_EQUITY_SERIES))]

        def _band_pct(row: object) -> str:
            """Price-band width as a percent, from the scrip master's limits.

            band = (upper - lower) / (upper + lower), i.e. the half-range about
            the midpoint. Verified against known values: a 20% band shows
            1073.85/715.95 → 20.0, a 10% band 1167.70/955.50 → 10.0.
            """
            try:
                hi = float(row.SM_UPPER_LIMIT)
                lo = float(row.SM_LOWER_LIMIT)
                if hi <= 0 or lo <= 0 or hi + lo == 0:
                    return ""
                return f"{(hi - lo) / (hi + lo) * 100:.1f}"
            except (TypeError, ValueError):
                return ""

        out: dict[str, dict[str, str]] = {}
        seen_isin: set[str] = set()
        for r in nse.itertuples(index=False):
            sym = str(r.UNDERLYING_SYMBOL).strip()
            if not sym or sym in out:
                continue
            out[sym] = {
                "security_id": str(r.SECURITY_ID),
                "exchange": "NSE_EQ",
                "fno": "1" if sym in fno else "0",
                "band_pct": _band_pct(r),
                "mtf_leverage": str(r.MTF_LEVERAGE or "").strip(),
            }
            seen_isin.add(str(r.ISIN))
        for r in bse.itertuples(index=False):
            sym = str(r.UNDERLYING_SYMBOL).strip()
            if not sym or sym in out or str(r.ISIN) in seen_isin:
                continue  # dual-listed → keep the NSE line
            out[sym] = {"security_id": str(r.SECURITY_ID), "exchange": "BSE_EQ"}
            seen_isin.add(str(r.ISIN))
        _log.info("dhan_universe_resolved", count=len(out))
        return out

    def circuit_safe(self, symbol: str, min_band_pct: Decimal) -> bool:
        """Is this scrip safe to hold intraday w.r.t. a hard circuit lock?

        True when the scrip is an F&O underlying (its band is dynamic — it
        widens after a cool-off instead of freezing) OR its hard band is at
        least ``min_band_pct`` wide. False only for narrow-band, non-F&O
        names, where a continued slide can lock the scrip and strand a long
        position until the auto square-off (Decision 029).

        Unknown symbol or missing band data ⇒ False (skip): for a filter whose
        job is avoiding untradeable names, absent evidence is not evidence of
        safety.
        """
        info = self.universe.get(symbol)
        if info is None:
            return False
        if info.get("fno") == "1":
            return True
        raw = info.get("band_pct") or ""
        if not raw:
            return False
        try:
            return Decimal(raw) >= min_band_pct
        except ArithmeticError:
            return False

    def max_leverage(self, symbol: str) -> Decimal | None:
        """Per-scrip leverage ceiling from the scrip master, or None.

        Leverage on NSE cash equity is graded PER SCRIP by risk, not flat:
        measured 2026-07-21 across the intraday-indian universes, the median
        ``MTF_LEVERAGE`` is 4.44x for NIFTY-100, 3.79x for Midcap 150 and
        3.06x for Smallcap 100 — and NOT ONE name in any of them reaches 5x.
        Sizing every position at a bucket-wide 5x would therefore over-order
        on essentially every trade and collect an RMS rejection.

        CAVEAT — this is the **MTF** (funded-delivery) figure; the scrip
        master carries no MIS/intraday column (``BUY_CO_MIN_MARGIN_PER`` and
        ``BUY_BO_MIN_MARGIN_PER`` are all zero, ``COVER_FLAG`` all null). Both
        numbers are graded off the same exchange risk parameters, and
        intraday risk is lower than overnight, so MTF leverage is expected to
        UNDER-state what MIS allows. That makes it a safe fallback, not a
        substitute for the real figure: ``Broker.required_margin`` remains the
        authority whenever the venue answers.

        0 (not MTF-eligible) and unparseable values return None, which the
        caller treats as "unknown" and degrades to 1x.
        """
        info = self.universe.get(symbol)
        if info is None:
            return None
        raw = (info.get("mtf_leverage") or "").strip()
        if not raw:
            return None
        try:
            lev = Decimal(raw)
        except ArithmeticError:
            return None
        return lev if lev > 1 else None

    def refresh_universe(self) -> int:
        """Force a scrip-master re-download (monthly, or after listing changes)."""
        self._universe = self._load_universe(force=True)
        return len(self._universe)

    # ── MarketData interface ────────────────────────────────────────────
    def get_ohlcv(
        self, symbol: str, interval: str, limit: int = 500
    ) -> list[OHLCVBar]:
        security_id, exchange = self._resolve(symbol)
        if interval == "1d":
            return self._daily(security_id, exchange, limit)
        minutes = _INTRADAY_MINUTES.get(interval)
        if minutes is None:
            raise ValueError(
                f"Unsupported Dhan interval {interval!r}; "
                f"valid: 1d, {list(_INTRADAY_MINUTES)}"
            )
        return self._intraday(security_id, exchange, minutes)

    def _daily(self, security_id: str, exchange: str, limit: int) -> list[OHLCVBar]:
        to_d = datetime.now(UTC).date()
        # ~1.5 calendar days per trading day covers weekends/holidays.
        from_d = to_d - timedelta(days=math.ceil(limit * 1.5) + 5)
        payload = {
            "securityId": security_id, "exchangeSegment": exchange,
            "instrument": "EQUITY", "expiryCode": 0,
            "fromDate": str(from_d), "toDate": str(to_d),
        }
        data = self._charts("historical", payload)
        return self._parse_candles(data)[-limit:]

    def _intraday(self, security_id: str, exchange: str, minutes: str) -> list[OHLCVBar]:
        today = datetime.now(UTC).date()
        payload = {
            "securityId": security_id, "exchangeSegment": exchange,
            "instrument": "EQUITY", "interval": minutes,
            "fromDate": str(today - timedelta(days=5)), "toDate": str(today),
        }
        return self._parse_candles(self._charts("intraday", payload))

    def _charts(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a charts request, resilient to token expiry AND rate limits.

        Dhan's Data API caps the historical/intraday charts endpoint at
        **5 requests/second** (confirmed from the API docs). The intraday
        scan fetches ~2 calls/symbol across a 99-name universe, so without
        both a per-request pace (``request_delay_seconds``, set for the live
        bot) AND a 429 backoff here, most of the morning cut 429'd and the
        scan silently saw an almost-empty universe (observed live 2026-07-22:
        95/99 symbols failed). The pace keeps us under the limit; this retry
        absorbs the occasional overflow and any burst from other callers
        sharing the client.
        """
        url = f"{self._base}/v2/charts/{endpoint}"
        headers = {"Content-Type": "application/json"}
        refreshed = False
        last: httpx.Response | None = None
        for attempt in range(_CHARTS_MAX_ATTEMPTS):
            if self._req_delay:
                time.sleep(self._req_delay)  # pace BEFORE the call, not after
            r = self._http.post(
                url,
                json=payload,
                headers={**headers, "access-token": self._token.token()},
            )
            last = r
            if r.status_code == 401 and not refreshed:
                # Token expired mid-flight → refresh once and retry immediately.
                self._token.invalidate()
                refreshed = True
                continue
            if r.status_code == 429:
                wait = _retry_after_seconds(r)
                if wait is None:
                    wait = min(
                        _CHARTS_BACKOFF_BASE * (2**attempt), _CHARTS_BACKOFF_MAX
                    )
                _log.warning(
                    "dhan_charts_rate_limited",
                    endpoint=endpoint,
                    attempt=attempt + 1,
                    wait_seconds=round(wait, 2),
                )
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        # Attempts exhausted — surface the last failure with its real status.
        assert last is not None
        last.raise_for_status()
        return last.json()

    @staticmethod
    def _parse_candles(data: dict[str, Any]) -> list[OHLCVBar]:
        ts = data.get("timestamp")
        if not ts:
            return []
        bars = [
            OHLCVBar(
                timestamp=datetime.fromtimestamp(t, tz=UTC),
                open=Decimal(str(o)), high=Decimal(str(h)),
                low=Decimal(str(low)), close=Decimal(str(c)),
                volume=Decimal(str(v)),
            )
            for t, o, h, low, c, v in zip(
                ts, data["open"], data["high"], data["low"],
                data["close"], data["volume"], strict=False,
            )
        ]
        bars.sort(key=lambda b: b.timestamp)
        return bars

    def get_ticker(self, symbol: str) -> Ticker:
        security_id, exchange = self._resolve(symbol)
        quotes = self._quote({exchange: [int(security_id)]})
        raw = quotes.get(exchange, {}).get(security_id) or quotes.get(
            exchange, {}
        ).get(int(security_id))
        if raw is None:
            raise ValueError(f"No Dhan quote for {symbol!r}")
        return self._parse_quote(symbol, raw)

    def get_tickers(self) -> list[Ticker]:
        """Quotes for the whole universe (batched ≤1000/req).

        NOTE: heavy over ~4,600 names — the equity scanner uses the
        daily-prepare path instead of this per-tick (see scanner engine).
        """
        out: list[Ticker] = []
        by_exch: dict[str, list[tuple[str, str]]] = {}
        for sym, info in self.universe.items():
            by_exch.setdefault(info["exchange"], []).append(
                (sym, info["security_id"])
            )
        for exchange, pairs in by_exch.items():
            for i in range(0, len(pairs), 1000):
                chunk = pairs[i:i + 1000]
                quotes = self._quote({exchange: [int(sid) for _, sid in chunk]})
                exq = quotes.get(exchange, {})
                for sym, sid in chunk:
                    raw = exq.get(sid) or exq.get(int(sid))
                    if raw is not None:
                        out.append(self._parse_quote(sym, raw))
        return out

    def _quote(self, body: dict[str, list[int]]) -> dict[str, Any]:
        r = self._http.post(
            f"{self._base}/v2/marketfeed/quote",
            json=body,
            headers={"access-token": self._token.token(),
                     "client-id": "", "Content-Type": "application/json"},
        )
        if r.status_code == 401:
            self._token.invalidate()
            r = self._http.post(
                f"{self._base}/v2/marketfeed/quote",
                json=body,
                headers={"access-token": self._token.token(),
                         "client-id": "", "Content-Type": "application/json"},
            )
        r.raise_for_status()
        return r.json().get("data", {})

    @staticmethod
    def _parse_quote(symbol: str, q: dict[str, Any]) -> Ticker:
        ohlc = q.get("ohlc", {}) or {}
        return Ticker(
            symbol=symbol,
            last_price=Decimal(str(q.get("last_price", 0) or 0)),
            volume_24h=Decimal(str(q.get("volume", 0) or 0)),
            mark_price=Decimal(str(q.get("last_price", 0) or 0)),
            raw={**q, "prev_close": ohlc.get("close")},
        )

    def get_funding_rate(self, symbol: str) -> FundingRate:
        raise NotImplementedError("Cash equities have no funding rate")

    # ── lifecycle ───────────────────────────────────────────────────────
    def close(self) -> None:
        if self._owns_http:
            self._http.close()
