"""
swing-indian DRY RUN — what would the 1h mean-reversion bucket trade right now?

Runs the REAL production code paths against LIVE Dhan data and prints the
decisions. **It never places an order.** ``place_order`` is not imported and not
called anywhere in this file; the only broker call is ``required_margin``, a
read-only margin quote.

    py -3.14 scripts/meanrev_dryrun.py                # the full 94-name set
    py -3.14 scripts/meanrev_dryrun.py --limit 15     # sample, for a quick check
    py -3.14 scripts/meanrev_dryrun.py --show-all     # print every symbol's dist

Best run just after a 1h bin closes on a trading day (10:16, 11:16, … 15:16 IST):
that is when the scan has a freshly completed bar to act on. Run it mid-bin and
you will see the previous bin's picture, which is correct behaviour.

WHAT IT ANSWERS — the things this bucket has never exercised live:
  1. Does a 90-day 15m pull succeed for all 94 names, and resample to a warm
     1h EMA20 series?
  2. Does the adjusted/unadjusted scale guard reject anything unexpected?
  3. Does the daily ATR14 read (and therefore the protective stop distance)
     resolve for every name?
  4. If something crossed −6.5%, what would the order size and MTF leverage be?

It does NOT verify order acceptance, the mean-touch exit, or the resting ATR
stop; only a real trade can do those.
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.brokers.dhan.client import DhanClient  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.data_sources.dhan import BOT_REQUEST_DELAY_SECONDS, DhanData  # noqa: E402
from src.shared.allocator.sizer import load_allocator_config  # noqa: E402
from src.shared.bucket import load_bucket  # noqa: E402
from src.shared.scanner.engine import load_scanner_config  # noqa: E402
from src.shared.scanner.meanrev import (  # noqa: E402
    IST,
    MeanRevConfig,
    daily_atr,
    evaluate,
    last_complete_bar_key,
    rank_top,
    resample_1h,
    scales_consistent,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only scan the first N")
    ap.add_argument("--show-all", action="store_true", help="print every symbol")
    args = ap.parse_args()

    bucket = load_bucket("swing-indian")
    scanner_cfg = load_scanner_config(bucket.scanner_yaml_path)
    cfg = MeanRevConfig.from_scanner_config(scanner_cfg)
    alloc = load_allocator_config(bucket.allocator_yaml_path)

    settings = get_settings()
    data = DhanData.from_settings(
        settings, request_delay_seconds=BOT_REQUEST_DELAY_SECONDS
    )
    broker = DhanClient.from_settings(
        data.resolve, settings, data_token_manager=data.token_manager
    )

    now = datetime.now(tz=IST)
    key = last_complete_bar_key(now)
    print(f"\nswing-indian dry run — {now:%Y-%m-%d %H:%M:%S} IST")
    print(f"mode={settings.trading_mode.value}  scanning for bar {key}")
    print(
        f"threshold=-{cfg.dist_threshold}%  ema{cfg.ema_len}  "
        f"stop={cfg.stop_atr_mult}xATR{cfg.atr_period}  "
        f"capital=Rs {bucket.config.capital_inr}  "
        f"margin/slot=Rs {bucket.config.capital_inr * alloc.per_symbol_cap}\n"
    )

    symbols = list(cfg.symbols)
    if cfg.fno_only:
        dropped = [s for s in symbols if data.universe.get(s, {}).get("fno") != "1"]
        if dropped:
            print(f"NOT F&O any more (excluded): {dropped}")
        symbols = [s for s in symbols if s not in dropped]
    if args.limit:
        symbols = symbols[: args.limit]

    started = _time.monotonic()
    signals, rejects, errors = [], [], []
    for i, sym in enumerate(symbols, start=1):
        try:
            i15 = data.get_ohlcv_history(sym, "15m", days=cfg.intraday_lookback_days)
            d1 = data.get_ohlcv(sym, "1d", limit=cfg.atr_period + 40)
        except Exception as e:  # noqa: BLE001
            errors.append((sym, type(e).__name__, str(e)[:80]))
            continue

        h1 = resample_1h(i15)
        why = None
        if not scales_consistent(i15, d1, cfg.scale_tolerance):
            why = "scale guard (split/bonus)"
        elif len(h1) < cfg.ema_len * 5:
            why = f"cold EMA ({len(h1)} of {cfg.ema_len * 5} 1h bars)"
        elif not len(h1):
            why = "no intraday bars"
        else:
            day = h1["ist_date"].iloc[-1]
            if daily_atr(d1, day, cfg.atr_period) is None:
                why = "no daily ATR (short history)"
        if why:
            rejects.append((sym, why))
            continue

        sig = evaluate(sym, i15, d1, cfg, want_bar_key=key)
        if sig is not None:
            signals.append(sig)
        if args.show_all and len(h1):
            last_key = f"{h1['ist_date'].iloc[-1]}#{int(h1['bin'].iloc[-1])}"
            print(f"  {sym:<12} bars={len(h1):>4} newest_bin={last_key}")
        if i % 20 == 0:
            print(f"  … {i}/{len(symbols)} ({_time.monotonic() - started:.0f}s)")

    print(f"\nscanned {len(symbols)} in {_time.monotonic() - started:.0f}s")
    if errors:
        print(f"\nFETCH ERRORS ({len(errors)}):")
        for sym, kind, msg in errors[:20]:
            print(f"  {sym:<12} {kind}: {msg}")
    if rejects:
        print(f"\nNOT EVALUABLE ({len(rejects)}):")
        for sym, why in rejects[:20]:
            print(f"  {sym:<12} {why}")

    if not signals:
        print("\nNo fresh -6.5% cross on this bar. That is the normal answer:")
        print("the backtest averaged ~2 entries a DAY across 94 names.")
        return 0

    chosen = rank_top(signals, cfg.universe_size)
    margin = bucket.config.capital_inr * alloc.per_symbol_cap
    print(f"\nCANDIDATES ({len(signals)} crossed, top {len(chosen)} would size):")
    for s in chosen:
        scrip_lev = data.max_leverage(s.symbol) or Decimal("1")
        lev = min(scrip_lev, bucket.config.leverage_max)
        qty = int(margin * lev / s.close)
        needed = None
        try:
            needed = broker.required_margin(
                s.symbol, "buy", Decimal(qty), s.close,
                product=bucket.config.product,
            )
        except Exception as e:  # noqa: BLE001
            needed = f"ERR {type(e).__name__}: {str(e)[:60]}"
        print(
            f"  {s.symbol:<12} dist={s.dist_pct:>7}%  close={s.close:>9}  "
            f"ema20={s.ema20:>9}  stop={s.close - s.stop_distance:>9} "
            f"(-{s.stop_distance})"
        )
        print(
            f"               qty={qty} @ {lev}x (scrip {scrip_lev}x)  "
            f"notional=Rs {int(qty * s.close)}  margin_quote={needed}"
        )
    print("\n(dry run — no orders were placed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
