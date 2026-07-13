"""
Blasting Momentum Swing v1 -- INTERIM Dhan sandbox/live runner (Phase-4 bridge).

This standalone tool executes the swing-indian bucket's spec
(src/strategies/swing/indian/{scanner.yaml, strategies/blasting_momentum.py})
on the Dhan API **before** the Phase-3/4 Dhan adapter + bucket runner land.
It deliberately lives in scripts/ -- OUTSIDE the deterministic bot loop -- and
must be retired once `swing-indian` is enabled in buckets.yaml.

Commands (see README.md for Task Scheduler setup):

    python scanner_live.py prepare   # ~18:00 IST: daily indicators -> shortlist
    python scanner_live.py scan      # 09:45 IST: confirm intraday, BUY top slots
    python scanner_live.py manage    # 15:15 IST: Supertrend-flip / 30d-cap SELLs
    python scanner_live.py status    # print open positions
    ... add --dry-run to log orders without sending them.
    ... add --refresh-universe to rebuild the NSE+BSE symbol map.

Universe: the ENTIRE tradeable NSE + BSE cash-equity universe (user decision
2026-07-10): NSE SEGMENT=E SERIES=EQ plus BSE series A/B/X/XT, deduplicated by
ISIN preferring the NSE listing (~4,000 symbols). NOTE: the backtest was run on
Nifty 500 -- whole-market microcaps are guarded only by the price/volume/
turnover floors. Watch live fill quality.

Design notes
------------
* Indicators are imported from the Backtesting Engine (BACKTEST_ENGINE_DIR) so
  live numbers are byte-identical to the backtest.
* Market DATA always uses the LIVE data API (api.dhan.co) with DHAN_ACCESS_TOKEN:
  the sandbox has no market data. ORDERS go to the env-selected base URL
  (sandbox: sandbox.dhan.co with the sandbox token/client id).
* `prepare` does the heavy pass (~4,000 symbols x 1 daily-history call,
  ~40-60 min: 0.25s/req delay + network round-trip x ~4.6k symbols) the evening
  before; `scan` then only needs intraday
  bars for the (small) shortlist, so 09:45 entries land within seconds.
* MTF orders on non-approved scrips are rejected by Dhan's RMS
  **asynchronously**: the POST returns 2xx with an orderId and the rejection
  only appears in the order book (day-one soak finding, 2026-07-13). Every
  order is therefore verified against the order book after placement; with
  MTF_FALLBACK_CNC=true a rejected MTF order is retried as CNC (1x).
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# --- Backtesting Engine reuse (indicators + scrip master) ----------------------
ENGINE_DIR = os.environ.get("BACKTEST_ENGINE_DIR",
                            r"D:/Claude_TVconnect2/Backtesting Engine")
sys.path.insert(0, ENGINE_DIR)
import pandas as pd                                      # noqa: E402
from backtest_engine import (calc_ema, calc_rsi, calc_cci,   # noqa: E402
                             calc_supertrend)
from nse_universe import fetch_dhan_scrip_master         # noqa: E402

# --- Config ---------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
STATE_DIR = Path(__file__).parent / "state"
UNIVERSE_F = STATE_DIR / "universe_all.json"
SHORTLIST_F = STATE_DIR / "shortlist.json"
POSITIONS_F = STATE_DIR / "positions.json"
TRADES_F = STATE_DIR / "trades.csv"
LOG_F = STATE_DIR / "scanner.log"
TOKEN_CACHE_F = STATE_DIR / "token_cache.json"

POSITION_SIZE = float(os.environ.get("POSITION_SIZE", 10_000))
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", 5))
MAX_DAYS = int(os.environ.get("MAX_DAYS", 30))
PRODUCT_TYPE = os.environ.get("PRODUCT_TYPE", "MTF")     # MTF (3x) | CNC
MTF_FALLBACK_CNC = os.environ.get("MTF_FALLBACK_CNC", "true").lower() == "true"
ST_PERIOD, ST_MULT = 10, 3.0

# v1 entry thresholds (mirror src/strategies/swing/indian/scanner.yaml)
PCT_MIN = 2.0
RSI_MIN = 65.0
CCI_MIN = 200.0
VOL_MIN = 20_000
PRICE_LO, PRICE_HI = 100.0, 2_000.0
TURNOVER_MIN = 500_000

DATA_BASE = "https://api.dhan.co"                        # market data: ALWAYS live
DATA_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")

# Auto-refresh creds (optional). Dhan capped directly-generated access tokens at
# 24h on 2025-10-01 (SEBI rule), so a static token in .env goes stale mid-cycle
# (prepare 18:00 -> next-day scan/manage spans >24h). If these are set, each run
# mints a fresh 24h token via TOTP instead of relying on DHAN_ACCESS_TOKEN.
AUTH_BASE = "https://auth.dhan.co"
DHAN_PIN = os.environ.get("DHAN_PIN", "")
DHAN_TOTP_SECRET = os.environ.get("DHAN_TOTP_SECRET", "")

DHAN_ENV = os.environ.get("DHAN_ENV", "sandbox").lower()
if DHAN_ENV == "sandbox":
    ORDER_BASE = "https://sandbox.dhan.co"
    ORDER_TOKEN = os.environ.get("DHAN_SANDBOX_ACCESS_TOKEN", "")
    CLIENT_ID = os.environ.get("DHAN_SANDBOX_CLIENT_ID", "")
else:
    ORDER_BASE = "https://api.dhan.co"
    ORDER_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
    CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")

# The live DATA account's client id (data token is always tied to the live
# account, even in sandbox order mode).
LIVE_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")

DRY_RUN = "--dry-run" in sys.argv
REQ_DELAY = 0.25                                          # data API politeness


def _log(msg: str):
    line = f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] {msg}"
    print(line, flush=True)
    # Task Scheduler discards stdout, so every run also appends to state/
    # scanner.log — the only forensic trail for scheduled runs.
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_F, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- Access-token refresh (Dhan 24h cap, 2025-10-01) ----------------------------

def refresh_access_token(client_id: str, pin: str, totp_secret: str) -> str | None:
    """Mint a fresh ~24h Dhan access token via the TOTP flow.

    Uses POST auth.dhan.co/app/generateAccessToken?dhanClientId&pin&totp, where
    ``totp`` is a live 6-digit code derived from the account's TOTP secret. The
    API key/secret is valid 12 months but still only issues 24h access tokens,
    so callers refresh on every run. Returns None on any failure (caller then
    falls back to the static token from .env).
    """
    if not (client_id and pin and totp_secret):
        return None
    try:
        import pyotp
    except ImportError:
        _log("pyotp not installed -- skipping token refresh (pip install pyotp)")
        return None
    for attempt in (1, 2, 3):
        try:
            code = pyotp.TOTP(totp_secret).now()
            r = requests.post(
                f"{AUTH_BASE}/app/generateAccessToken",
                params={"dhanClientId": client_id, "pin": pin, "totp": code},
                timeout=30,
            )
            r.raise_for_status()
            tok = r.json().get("accessToken")
            if not tok:
                _log(f"token refresh: no accessToken in response ({r.text[:200]})")
                return None
            _log("access token refreshed (valid ~24h)")
            return tok
        except Exception as e:                    # noqa: BLE001
            _log(f"token refresh attempt {attempt}/3 failed ({e})")
            if attempt < 3:
                time.sleep(5 * attempt)
    return None


def resolve_tokens() -> None:
    """Refresh the live DATA token (and the live ORDER token when DHAN_ENV=live).

    No-op unless DHAN_PIN + DHAN_TOTP_SECRET + DHAN_CLIENT_ID are all set, so an
    existing static-token setup keeps working unchanged. Sandbox ORDER tokens are
    DevPortal-issued and are NOT refreshed here (see .env.example).
    """
    global DATA_TOKEN, ORDER_TOKEN
    fresh = refresh_access_token(LIVE_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET)
    if fresh:
        DATA_TOKEN = fresh
        if DHAN_ENV != "sandbox":
            ORDER_TOKEN = fresh                   # live orders share the data token
        _save(TOKEN_CACHE_F, {"token": fresh,
                              "minted_at": datetime.now(timezone.utc).isoformat()})
        return
    # Refresh failed (a 15:15 run died on exactly this, 2026-07-13): fall back
    # to the last minted token while it's still inside Dhan's 24h validity.
    # The static .env DHAN_ACCESS_TOKEN, if set, remains the final fallback.
    cache = _load(TOKEN_CACHE_F, None)
    if cache:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(cache["minted_at"])).total_seconds() / 3600
        if age_h < 23:
            _log(f"token refresh unavailable -- using cached token ({age_h:.1f}h old)")
            DATA_TOKEN = cache["token"]
            if DHAN_ENV != "sandbox":
                ORDER_TOKEN = cache["token"]


# --- Universe: entire tradeable NSE + BSE ---------------------------------------

_BSE_EQUITY_SERIES = {"A", "B", "X", "XT", "T"}


def resolve_universe_all(force: bool = False) -> dict[str, dict]:
    """symbol -> {security_id, exchange} for all NSE EQ + BSE-only equities.

    Dedupe by ISIN, preferring the NSE listing (liquidity + MTF coverage).
    Cached in state/universe_all.json; refresh with --refresh-universe.
    """
    if UNIVERSE_F.exists() and not force:
        with open(UNIVERSE_F) as f:
            return json.load(f)

    _log("resolving full NSE+BSE universe from Dhan scrip master ...")
    m = fetch_dhan_scrip_master()
    e = m[m["SEGMENT"] == "E"]
    nse = e[(e["EXCH_ID"] == "NSE") & (e["SERIES"] == "EQ")
            & (e["INSTRUMENT"] == "EQUITY")]
    bse = e[(e["EXCH_ID"] == "BSE") & (e["INSTRUMENT"] == "EQUITY")
            & (e["SERIES"].isin(_BSE_EQUITY_SERIES))]

    out: dict[str, dict] = {}
    seen_isin: set[str] = set()
    for r in nse.itertuples(index=False):
        sym = str(r.UNDERLYING_SYMBOL).strip()
        if not sym or sym in out:
            continue
        out[sym] = {"security_id": str(r.SECURITY_ID), "exchange": "NSE_EQ"}
        seen_isin.add(str(r.ISIN))
    added_bse = 0
    for r in bse.itertuples(index=False):
        sym = str(r.UNDERLYING_SYMBOL).strip()
        if not sym or sym in out or str(r.ISIN) in seen_isin:
            continue                        # dual-listed -> keep the NSE line
        out[sym] = {"security_id": str(r.SECURITY_ID), "exchange": "BSE_EQ"}
        seen_isin.add(str(r.ISIN))
        added_bse += 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(UNIVERSE_F, "w") as f:
        json.dump(out, f, indent=1)
    _log(f"universe: {len(out)} symbols ({len(out) - added_bse} NSE + {added_bse} BSE-only)")
    return out


# --- Dhan market data (live API) --------------------------------------------------

def _charts(endpoint: str, payload: dict) -> dict:
    r = requests.post(f"{DATA_BASE}/v2/charts/{endpoint}", json=payload,
                      headers={"Content-Type": "application/json",
                               "access-token": DATA_TOKEN}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_daily(security_id: str, exchange: str, days: int = 120) -> pd.DataFrame | None:
    to_d = datetime.now(IST).date()
    payload = {"securityId": security_id, "exchangeSegment": exchange,
               "instrument": "EQUITY", "expiryCode": 0,
               "fromDate": str(to_d - timedelta(days=days)), "toDate": str(to_d)}
    d = _charts("historical", payload)
    if not d.get("timestamp"):
        return None
    df = pd.DataFrame({k: d[k] for k in ("open", "high", "low", "close", "volume")})
    df["date"] = [datetime.fromtimestamp(t, tz=timezone.utc) for t in d["timestamp"]]
    return df.sort_values("date").reset_index(drop=True)


def fetch_today_15m(security_id: str, exchange: str) -> pd.DataFrame | None:
    today = datetime.now(IST).date()
    payload = {"securityId": security_id, "exchangeSegment": exchange,
               "instrument": "EQUITY", "interval": "15",
               "fromDate": str(today), "toDate": str(today)}
    d = _charts("intraday", payload)
    if not d.get("timestamp"):
        return None
    df = pd.DataFrame({k: d[k] for k in ("open", "high", "low", "close", "volume")})
    df["date"] = [datetime.fromtimestamp(t, tz=timezone.utc) for t in d["timestamp"]]
    df["hhmm"] = (df["date"] + timedelta(hours=5, minutes=30)).dt.strftime("%H:%M")
    return df.sort_values("date").reset_index(drop=True)


# --- Dhan orders (sandbox/live base) -----------------------------------------------

def _post_order(payload: dict) -> requests.Response:
    return requests.post(f"{ORDER_BASE}/v2/orders", json=payload,
                         headers={"Content-Type": "application/json",
                                  "access-token": ORDER_TOKEN}, timeout=30)


def _order_status(order_id: str) -> dict | None:
    try:
        r = requests.get(f"{ORDER_BASE}/v2/orders/{order_id}",
                         headers={"access-token": ORDER_TOKEN,
                                  "client-id": CLIENT_ID}, timeout=30)
        r.raise_for_status()
        d = r.json()
        if isinstance(d, list):
            d = d[0] if d else None
        return d if isinstance(d, dict) else None
    except Exception:                             # noqa: BLE001
        return None


_ORDER_DEAD = {"REJECTED", "CANCELLED", "EXPIRED"}


def _post_and_verify(payload: dict) -> tuple[dict | None, str]:
    """POST an order, then confirm the order book didn't reject it.

    Dhan's RMS rejects asynchronously: the POST returns 2xx with an orderId
    and e.g. "MTF is not permitted for this Scrip" only shows up in the order
    book afterwards. Returns (response, "") on success, (None, reason) on
    HTTP-level or order-book rejection.
    """
    r = _post_order(payload)
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code} {r.text[:200]}"
    resp = r.json()
    status = str(resp.get("orderStatus", "") or "")
    detail: dict = {}
    for wait in (1, 2, 3):
        if status not in ("", "TRANSIT", "PENDING"):
            break
        time.sleep(wait)
        detail = _order_status(str(resp.get("orderId", ""))) or detail
        status = str(detail.get("orderStatus", status) or status)
    if status in _ORDER_DEAD:
        return None, detail.get("omsErrorDescription") or status
    resp["orderStatus"] = status or "PENDING"
    return resp, ""


def place_order(security_id: str, exchange: str, side: str, qty: int,
                symbol: str, product: str | None = None) -> dict | None:
    product = product or PRODUCT_TYPE
    payload = {
        "dhanClientId": CLIENT_ID,
        "transactionType": side,                 # BUY | SELL
        "exchangeSegment": exchange,             # NSE_EQ | BSE_EQ
        "productType": product,                  # MTF (3x funding) | CNC
        "orderType": "MARKET",
        "validity": "DAY",
        "securityId": security_id,
        "quantity": qty,
        "price": 0,
        "disclosedQuantity": 0,
        "afterMarketOrder": False,
    }
    if DRY_RUN:
        _log(f"DRY-RUN {side} {qty} x {symbol} [{exchange}] ({product}) -> {ORDER_BASE}")
        return {"orderId": "dry-run", "orderStatus": "DRY_RUN", "product": product}
    resp, err = _post_and_verify(payload)
    if resp is None and product == "MTF" and MTF_FALLBACK_CNC:
        _log(f"MTF rejected for {symbol} ({err}) -- retrying as CNC (1x)")
        payload["productType"] = "CNC"
        resp, err = _post_and_verify(payload)
    if resp is None:
        _log(f"ORDER REJECTED {side} {symbol}: {err}")
        return None
    resp["product"] = payload["productType"]
    _log(f"ORDER OK {side} {qty} x {symbol} ({payload['productType']}): "
         f"id={resp.get('orderId')} status={resp.get('orderStatus')}")
    return resp


# --- State --------------------------------------------------------------------------

def _load(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def _save(path: Path, obj):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def _log_trade(row: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    new = not TRADES_F.exists()
    with open(TRADES_F, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


# --- prepare: evening shortlist --------------------------------------------------------

def cmd_prepare():
    secmap = resolve_universe_all(force="--refresh-universe" in sys.argv)
    _log(f"prepare: {len(secmap)} symbols (full NSE+BSE) -- expect ~40-60 min")
    shortlist, scanned, errors = [], 0, 0
    for sym, info in secmap.items():
        try:
            df = fetch_daily(info["security_id"], info["exchange"])
            time.sleep(REQ_DELAY)
            if df is None or len(df) < 40:
                continue
            scanned += 1
            c = float(df["close"].iloc[-1])
            if not (PRICE_LO <= c <= PRICE_HI):      # cheap gate before indicators
                continue
            rsi = calc_rsi(df["close"], 14)
            r0, r1 = float(rsi.iloc[-1]), float(rsi.iloc[-2])
            if not (r0 >= RSI_MIN and r0 > r1):
                continue
            ema10 = calc_ema(df["close"], 10)
            ema20 = calc_ema(df["close"], 20)
            if not float(ema10.iloc[-1]) > float(ema20.iloc[-1]):
                continue
            cci = calc_cci(df, 14)
            if not float(cci.iloc[-1]) >= CCI_MIN:
                continue
            st = calc_supertrend(df, ST_PERIOD, ST_MULT)
            shortlist.append({
                "symbol": sym, "security_id": info["security_id"],
                "exchange": info["exchange"],
                "prev_close": c, "rsi": round(r0, 2),
                "cci": round(float(cci.iloc[-1]), 1),
                "supertrend": round(float(st.iloc[-1]), 2),
            })
        except Exception as e:                    # noqa: BLE001 -- keep sweeping
            errors += 1
            if errors <= 5:
                _log(f"  {sym}: {e}")
    _save(SHORTLIST_F, {"date": str(datetime.now(IST).date()),
                        "universe": "ALL_NSE_BSE", "candidates": shortlist})
    _log(f"prepare done: scanned {scanned}, shortlist {len(shortlist)}, errors {errors}")


# --- scan: 09:45 entries -----------------------------------------------------------------

def cmd_scan():
    sl = _load(SHORTLIST_F, None)
    if not sl:
        _log("scan: no shortlist -- run `prepare` the evening before. Aborting.")
        return
    age = (datetime.now(IST).date() - date.fromisoformat(sl["date"])).days
    if age > 3:
        _log(f"scan: shortlist is {age} days old -- run `prepare` again. Aborting.")
        return

    positions = _load(POSITIONS_F, {})
    free = MAX_POSITIONS - len(positions)
    if free <= 0:
        _log("scan: all slots full, nothing to do.")
        return

    cands = []
    for c in sl["candidates"]:
        if c["symbol"] in positions:
            continue
        try:
            g = fetch_today_15m(c["security_id"], c["exchange"])
            time.sleep(REQ_DELAY)
        except Exception as e:                    # noqa: BLE001
            _log(f"  {c['symbol']}: intraday fetch failed ({e})")
            continue
        if g is None or len(g) < 2:
            continue
        before = g[g["hhmm"] < "09:45"]
        at = g[g["hhmm"] >= "09:45"]
        if before.empty:
            continue
        price = float(at.iloc[0]["open"]) if not at.empty else float(before.iloc[-1]["close"])
        cum_vol = float(before["volume"].sum())
        pct = (price / c["prev_close"] - 1.0) * 100.0
        if (pct >= PCT_MIN and cum_vol >= VOL_MIN
                and PRICE_LO <= price <= PRICE_HI
                and price * cum_vol >= TURNOVER_MIN):
            cands.append({**c, "price": price, "pct": pct, "cum_vol": cum_vol})

    cands.sort(key=lambda x: x["pct"], reverse=True)
    _log(f"scan: {len(cands)} candidates pass; {free} free slots")
    for c in cands[:free]:
        qty = math.floor(POSITION_SIZE / c["price"])
        if qty < 1:
            continue
        resp = place_order(c["security_id"], c["exchange"], "BUY", qty, c["symbol"])
        if resp is None:
            continue
        _log_trade({"ts": datetime.now(IST).isoformat(), "action": "BUY",
                    "symbol": c["symbol"], "exchange": c["exchange"], "qty": qty,
                    "price": round(c["price"], 2), "reason": "scan",
                    "product": resp.get("product", PRODUCT_TYPE),
                    "env": DHAN_ENV, "dry_run": DRY_RUN})
        if DRY_RUN:
            continue                              # dry-run: log only, never touch state
        positions[c["symbol"]] = {
            "security_id": c["security_id"], "exchange": c["exchange"],
            "qty": qty, "entry_price": round(c["price"], 2),
            "entry_date": str(datetime.now(IST).date()),
            "pct_at_entry": round(c["pct"], 2),
            "product": resp.get("product", PRODUCT_TYPE),
        }
    if not DRY_RUN:
        _save(POSITIONS_F, positions)
    _log(f"scan done: {len(positions)} open positions"
         + (" (dry-run: state not written)" if DRY_RUN else ""))


# --- manage: 15:15 exits -------------------------------------------------------------------

def cmd_manage():
    positions = _load(POSITIONS_F, {})
    if not positions:
        _log("manage: no open positions.")
        return
    today = datetime.now(IST).date()
    closed = []
    for sym, p in positions.items():
        try:
            df = fetch_daily(p["security_id"], p["exchange"])
            g = fetch_today_15m(p["security_id"], p["exchange"])
            time.sleep(REQ_DELAY)
        except Exception as e:                    # noqa: BLE001
            _log(f"  {sym}: data fetch failed ({e}) -- skipping today")
            continue
        if df is None:
            continue
        # Append today's forming daily bar from intraday so the Supertrend flip is
        # evaluated on TODAY's close (the backtest exits on the daily close).
        if g is not None and len(g) and str(df["date"].iloc[-1].date()) != str(today):
            df = pd.concat([df, pd.DataFrame([{
                "date": pd.Timestamp(datetime.now(timezone.utc)),
                "open": float(g.iloc[0]["open"]), "high": float(g["high"].max()),
                "low": float(g["low"].min()), "close": float(g.iloc[-1]["close"]),
                "volume": float(g["volume"].sum())}])], ignore_index=True)
        st = float(calc_supertrend(df, ST_PERIOD, ST_MULT).iloc[-1])
        close = float(df["close"].iloc[-1])
        days_held = (today - date.fromisoformat(p["entry_date"])).days
        reason = None
        if close < st:
            reason = "st_flip"
        elif days_held >= MAX_DAYS:
            reason = "max_days"
        _log(f"  {sym}: close={close:.2f} ST={st:.2f} held={days_held}d -> "
             f"{reason or 'HOLD'}")
        if reason:
            if place_order(p["security_id"], p["exchange"], "SELL", p["qty"],
                           sym, product=p.get("product", PRODUCT_TYPE)) is None:
                continue
            _log_trade({"ts": datetime.now(IST).isoformat(), "action": "SELL",
                        "symbol": sym, "exchange": p["exchange"], "qty": p["qty"],
                        "price": round(close, 2), "reason": reason,
                        "product": p.get("product", PRODUCT_TYPE),
                        "env": DHAN_ENV, "dry_run": DRY_RUN})
            closed.append(sym)
    if not DRY_RUN:
        for sym in closed:
            positions.pop(sym, None)
        _save(POSITIONS_F, positions)
    verb = "would close" if DRY_RUN else "closed"
    _log(f"manage done: {verb} {len(closed)}, still open {len(positions)}")


def cmd_status():
    positions = _load(POSITIONS_F, {})
    _log(f"env={DHAN_ENV} product={PRODUCT_TYPE} slots={len(positions)}/{MAX_POSITIONS}")
    for sym, p in positions.items():
        held = (datetime.now(IST).date() - date.fromisoformat(p["entry_date"])).days
        print(f"  {sym:<12} [{p.get('exchange','NSE_EQ')}] qty={p['qty']:<5} "
              f"entry={p['entry_price']:<9} {p['entry_date']} ({held}d) "
              f"{p.get('product','')}")


if __name__ == "__main__":
    cmds = {"prepare": cmd_prepare, "scan": cmd_scan,
            "manage": cmd_manage, "status": cmd_status}
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or args[0] not in cmds:
        print("Usage: python scanner_live.py {prepare|scan|manage|status} "
              "[--dry-run] [--refresh-universe]")
        sys.exit(1)
    resolve_tokens()        # mint a fresh 24h token if refresh creds are set
    if not DATA_TOKEN:
        _log("No market-data token: set DHAN_ACCESS_TOKEN, or DHAN_PIN + "
             "DHAN_TOTP_SECRET + DHAN_CLIENT_ID for auto-refresh. See .env.example")
        sys.exit(1)
    if DHAN_ENV != "sandbox" and not ORDER_TOKEN:
        _log("DHAN_ENV=live but no order token (DHAN_ACCESS_TOKEN / refresh creds).")
        sys.exit(1)
    try:
        cmds[args[0]]()
    except Exception:                             # noqa: BLE001
        _log(f"FATAL in {args[0]}:\n{traceback.format_exc()}")
        sys.exit(1)
