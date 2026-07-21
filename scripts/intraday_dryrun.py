"""
intraday-indian DRY RUN — what would this bucket trade, right now?

Runs the REAL production code paths against LIVE Dhan data and prints the
decisions. **It never places an order.** ``place_order`` is not imported and
not called anywhere in this file; the only broker call is
``required_margin``, which is a read-only margin quote.

    py -3.14 scripts/intraday_dryrun.py                 # validated NIFTY-100 set
    py -3.14 scripts/intraday_dryrun.py --scanner broad # the 235-name set
    py -3.14 scripts/intraday_dryrun.py --limit 20      # sample, for a quick check

Best run between 09:35 and 10:30 IST on a trading day — that is the window
where the morning cut is complete AND a reversal candle can still be actionable.
Run it earlier and the 09:25 bar will not exist yet; run it later and any signal
will read as stale (which is correct behaviour, not a bug).

WHAT IT ANSWERS — the things that have never been exercised live:
  1. Does the morning gap screen run against live Dhan 5m/1D data?
  2. Does the circuit filter exclude what we expect?
  3. Does ``/v2/margincalculator`` answer — and what leverage do we ACTUALLY get?
  4. Would a reversal candle be detected and sized?

It does NOT verify order acceptance or the 15:15 square-off; only a real
trade can do that.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.brokers.dhan.client import DhanClient  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.data_sources.dhan import DhanData  # noqa: E402
from src.shared.allocator.sizer import load_allocator_config  # noqa: E402
from src.shared.bucket import load_bucket  # noqa: E402
from src.shared.scanner.engine import load_scanner_config  # noqa: E402
from src.shared.scanner.gap_reversal import (  # noqa: E402
    GapReversalConfig,
    gap_screen,
    ist_date,
    rank_top,
)
from src.strategies.intraday.indian.strategies.gap_down_reversal import (  # noqa: E402
    GapDownReversal,
)

IST = __import__("src.shared.scanner.gap_reversal", fromlist=["IST"]).IST


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scanner", default="", help='"" (NIFTY-100) or "broad"')
    ap.add_argument("--limit", type=int, default=0, help="sample N symbols only")
    args = ap.parse_args()

    settings = get_settings()
    bucket = load_bucket("intraday-indian")
    scfg = load_scanner_config(bucket.scanner_yaml_path_for(args.scanner))
    acfg = load_allocator_config(bucket.allocator_yaml_path_for(args.scanner))
    gcfg = GapReversalConfig.from_scanner_config(scfg)

    now_ist = datetime.now(tz=IST)
    print("=" * 72)
    print("intraday-indian DRY RUN — no orders will be placed")
    print(f"  now                {now_ist:%Y-%m-%d %H:%M:%S} IST")
    print(f"  TRADING_MODE       {settings.trading_mode.value}")
    print(f"  scanner set        {args.scanner or '(default NIFTY-100, validated)'}")
    print(f"  bucket enabled     {bucket.config.enabled}  "
          f"(dry run works either way)")
    slot = bucket.config.capital_inr * acfg.per_symbol_cap
    print(f"  capital / cap      ₹{bucket.config.capital_inr:,} × "
          f"{acfg.per_symbol_cap} = ₹{slot:,.0f} margin/slot")
    print(f"  product / lev cap  {bucket.config.product} / {bucket.config.leverage_max}x")
    print("=" * 72)

    data = DhanData.from_settings(settings)

    # ---- 1. universe + circuit filter ---------------------------------
    symbols = list(gcfg.symbols)
    if gcfg.min_circuit_band_pct > 0:
        safe = [s for s in symbols if data.circuit_safe(s, gcfg.min_circuit_band_pct)]
        excluded = sorted(set(symbols) - set(safe))
        print(f"\n[1] CIRCUIT FILTER  kept {len(safe)}/{len(symbols)}")
        if excluded:
            print(f"    excluded: {', '.join(excluded)}")
        symbols = safe
    else:
        print(f"\n[1] CIRCUIT FILTER  off for this set ({len(symbols)} symbols)")

    if args.limit:
        symbols = symbols[: args.limit]
        print(f"    --limit {args.limit} → sampling {len(symbols)} symbols")

    # ---- 2. morning gap screen ----------------------------------------
    print(f"\n[2] GAP SCREEN  fetching 5m + 1D for {len(symbols)} symbols…")
    day = None
    cands, errors = [], []
    for i, sym in enumerate(symbols, 1):
        try:
            intraday = data.get_ohlcv(sym, "5m")
            daily = data.get_ohlcv(sym, "1d", limit=gcfg.atr_period + 30)
        except Exception as e:
            errors.append((sym, type(e).__name__, str(e)[:60]))
            continue
        if not intraday:
            errors.append((sym, "NoData", "empty 5m series"))
            continue
        if day is None:
            day = ist_date(max(intraday, key=lambda b: b.timestamp))
            print(f"    latest session in data: {day}")
        c = gap_screen(sym, intraday, daily, day, gcfg)
        if c is not None:
            cands.append(c)
        if i % 25 == 0:
            print(f"    …{i}/{len(symbols)}  ({len(cands)} passing so far)")

    print(f"    fetch errors: {len(errors)}")
    for sym, kind, msg in errors[:5]:
        print(f"      {sym}: {kind} {msg}")

    chosen = rank_top(cands, gcfg.universe_size)
    print(f"\n    {len(cands)} gapped down 3-12% with a decisive first 15m")
    print(f"    morning cut (top {gcfg.universe_size}):")
    if not chosen:
        print("      — none. Zero-signal days are NORMAL here (~0.8 trades/week).")
    for c in chosen:
        print(f"      {c.symbol:14s} gap {c.gap_pct:+6.2f}%  "
              f"body/ATR {c.body_atr_ratio:.2f}  open ₹{c.open_0915}")

    # ---- 3. reversal candle -------------------------------------------
    strat = GapDownReversal()
    print("\n[3] REVERSAL CANDLE  (entry window 09:30–10:30 IST)")
    entries = []
    for c in chosen:
        try:
            bars = data.get_ohlcv(c.symbol, "5m")
        except Exception as e:
            print(f"      {c.symbol:14s} fetch failed: {e}")
            continue
        hit = strat._first_reversal(bars)
        if hit is None:
            print(f"      {c.symbol:14s} no engulfing/hammer yet")
            continue
        idx, pattern = hit
        today = strat._today_bars(bars)
        age = len(today) - 1 - idx
        state = "ACTIONABLE" if age <= 1 else f"stale ({age} bars ago)"
        print(f"      {c.symbol:14s} {pattern:16s} {state}")
        if age <= 1:
            entries.append(c)

    # ---- 4. margin preflight + real leverage ---------------------------
    print("\n[4] MARGIN PREFLIGHT  — does /v2/margincalculator answer?")
    try:
        client = DhanClient.from_settings(
            data.resolve, settings, data_token_manager=data.token_manager
        )
    except Exception as e:
        print(f"    could not build broker client: {e}")
        return 1

    margin_slot = bucket.config.capital_inr * acfg.per_symbol_cap
    probe = [c.symbol for c in (chosen or [])][:5] or symbols[:3]
    print(f"    probing {len(probe)} symbol(s) at a ₹{margin_slot:,.0f} margin slot\n")
    print(f"    {'symbol':14s} {'price':>10s} {'scrip lev':>10s} "
          f"{'venue says':>12s} {'→ effective':>12s}")
    answered = 0
    for sym in probe:
        try:
            bars = data.get_ohlcv(sym, "5m")
            price = Decimal(str(max(bars, key=lambda b: b.timestamp).close))
        except Exception as e:  # noqa: S112 — a probe failure is informational
            print(f"    {sym:14s} price fetch failed: {type(e).__name__}")
            continue
        scrip_lev = data.max_leverage(sym)
        want_qty = (margin_slot * bucket.config.leverage_max / price).to_integral_value()
        needed = client.required_margin(
            sym, "buy", want_qty, price, product=bucket.config.product
        )
        if needed is not None and needed > 0:
            answered += 1
            eff = (want_qty * price) / needed
            venue = f"₹{needed:,.0f}"
            eff_s = f"{min(eff, bucket.config.leverage_max):.2f}x"
        else:
            venue = "no answer"
            eff_s = (
                f"{min(scrip_lev, bucket.config.leverage_max):.2f}x (fallback)"
                if scrip_lev
                else "1.00x (unknown)"
            )
        print(f"    {sym:14s} {price:>10} {str(scrip_lev or '—'):>10s} "
              f"{venue:>12s} {eff_s:>12s}")

    # ---- verdict -------------------------------------------------------
    print("\n" + "=" * 72)
    print("VERDICT")
    print(f"  gap screen ran ......... {'YES' if day else 'NO — no data returned'}")
    print(f"  candidates today ....... {len(cands)} (cut to {len(chosen)})")
    print(f"  actionable entries ..... {len(entries)}")
    if answered:
        print(f"  margin calculator ...... WORKS ({answered}/{len(probe)} answered)")
        print("     → live sizing will use exact per-scrip leverage.")
    else:
        print("  margin calculator ...... NO ANSWER")
        print("     → live sizing falls back to the scrip-master leverage above.")
        print("       Still leveraged, just possibly conservative.")
    print("\n  NO ORDERS WERE PLACED.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
