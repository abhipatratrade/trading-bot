"""Parity check: does this repo's port reproduce the frozen 1h mean-rev backtest?

Replays the ported signal logic (``shared/scanner/meanrev``) over the SAME
cached Dhan 15m/1D CSVs the Backtesting Engine used, and checks that every
trade in the frozen full-window run is reproduced:

  * the FRESH cross fires on the same 1h bin the backtest signalled on;
  * the resulting entry bar (the bin AFTER it) matches the backtest's
    ``entry_time``;
  * the protective stop distance (3.5 × daily ATR14 as of the prior close)
    matches the engine's, within a rupee.

    py -3.14 scripts/meanrev_1h_parity.py

Result 2026-07-27: **208/214 on all three axes.** The six accepted misses are
all the EMA20 warm-up guard, and all sit at a data boundary the live bot never
sees:

  * MCX / BHEL / IDEA / POWERINDIA / IREDA, all 2024-06-04 11:15 (the election
    crash) — the cached 15m series starts 2024-05-15, so only 96 of the required
    100 1h bins exist. The backtest signalled off an EMA seeded 14 sessions
    earlier; live, ``evaluate`` refuses rather than trade a cold mean.
  * WAAREEENER 2024-11-11 — a fresh IPO with 11 daily bars, so the daily ATR14
    (and therefore the protective stop) does not exist yet either.

Together they are ₹13,660 of the run's ₹163,636 net (8.3%), all winners, so the
port is CONSERVATIVE here, not broken. Live this only ever bites a recent
listing, which is exactly where a seeded EMA and a missing stop argue for
sitting out.

NOT a unit test: it reads the sibling Backtesting Engine repo, which CI does
not have. Re-run it by hand after any change to the resample, the EMA/ATR math,
or the frozen config.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"D:\Claude_TVconnect2\trading-bot")

from src.data_sources.base import OHLCVBar  # noqa: E402
from src.shared.scanner import indicators as ind  # noqa: E402
from src.shared.scanner.meanrev import (  # noqa: E402
    IST,
    MeanRevConfig,
    bar_key,
    evaluate,
    resample_1h,
)

ENGINE = Path(r"D:\Claude_TVconnect2\Backtesting Engine")
RUN = ENGINE / (
    "results/scanners/"
    "nifty_midcap_150_meanrev_1h_midcap_FINAL_fno_5perday_MTFlev_20260724_025002.json"
)

CFG = MeanRevConfig(
    universe_size=5,
    symbols=(),
    ema_len=20,
    dist_threshold=Decimal("6.5"),
    atr_period=14,
    stop_atr_mult=Decimal("3.5"),
    max_hold_days=20,
    daily_entry_cap=5,
    intraday_lookback_days=90,
    scale_tolerance=Decimal("0.03"),
    fno_only=True,
)

_CACHE: dict[tuple[str, str], list[OHLCVBar] | None] = {}


def load(symbol: str, tf: str) -> list[OHLCVBar] | None:
    key = (symbol, tf)
    if key in _CACHE:
        return _CACHE[key]
    p = ENGINE / "data" / "indian" / f"{symbol}_{tf}.csv"
    if not p.exists():
        _CACHE[key] = None
        return None
    df = pd.read_csv(p, parse_dates=["date"])
    bars = [
        OHLCVBar(
            timestamp=r.date.to_pydatetime().astimezone(UTC)
            if r.date.tzinfo
            else r.date.to_pydatetime().replace(tzinfo=UTC),
            open=Decimal(str(r.open)),
            high=Decimal(str(r.high)),
            low=Decimal(str(r.low)),
            close=Decimal(str(r.close)),
            volume=Decimal(str(r.volume)),
        )
        for r in df.itertuples()
    ]
    _CACHE[key] = bars
    return bars


def signal_bin_for_entry(entry_ist: datetime, h1: pd.DataFrame) -> tuple | None:
    """The (date, bin) whose NEXT bin opens at ``entry_ist``.

    The backtest fills at the open of the bar after the signal bar, so we invert
    that: find the row whose ts_ist equals the entry, and step back one.
    """
    matches = h1.index[h1["ts_ist"] == entry_ist].tolist()
    if not matches:
        return None
    i = matches[0]
    if i == 0:
        return None
    return h1["ist_date"].iloc[i - 1], int(h1["bin"].iloc[i - 1])


def main() -> int:
    trades = json.load(open(RUN))["trades"]
    print(f"Backtest trades to reproduce: {len(trades)}\n")

    cross_ok = entry_ok = stop_ok = 0
    missing = 0
    failures: list[tuple] = []

    for t in trades:
        sym = t["symbol"]
        i15, d1 = load(sym, "15m"), load(sym, "1D")
        if i15 is None or d1 is None:
            missing += 1
            continue

        entry_ist = datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M").replace(
            tzinfo=IST
        )
        h1 = resample_1h(i15)
        found = signal_bin_for_entry(entry_ist, h1)
        if found is None:
            failures.append((sym, t["entry_time"], "NO SUCH ENTRY BAR", ""))
            continue
        sig_day, sig_bin = found
        want = bar_key(sig_day, sig_bin)

        # Truncate the series at the signal bar — live, nothing later exists.
        cutoff = entry_ist.astimezone(UTC)
        past15 = [b for b in i15 if b.timestamp < cutoff]
        past1d = [b for b in d1 if b.timestamp < cutoff]

        sig = evaluate(sym, past15, past1d, CFG, want_bar_key=want)
        if sig is None:
            failures.append(
                (sym, t["entry_time"], "NO CROSS", f"backtest dist={t['dist_signal']}")
            )
            continue
        cross_ok += 1

        # dist agreement (the engine rounds to 1dp in its trade log)
        if abs(float(sig.dist_pct) - float(t["dist_signal"])) > 0.15:
            failures.append(
                (
                    sym,
                    t["entry_time"],
                    "DIST MISMATCH",
                    f"ours={sig.dist_pct} theirs={t['dist_signal']}",
                )
            )
        else:
            entry_ok += 1

        # stop distance = 3.5 × daily ATR14 as of the prior settled close
        engine_atr = _engine_atr(past1d, sig_day)
        if engine_atr is None:
            continue
        want_stop = engine_atr * float(CFG.stop_atr_mult)
        if abs(float(sig.stop_distance) - want_stop) <= max(0.01, want_stop * 0.001):
            stop_ok += 1
        else:
            failures.append(
                (
                    sym,
                    t["entry_time"],
                    "STOP MISMATCH",
                    f"ours={sig.stop_distance} theirs={want_stop:.4f}",
                )
            )

    total = len(trades) - missing
    print(f"fresh cross reproduced : {cross_ok}/{total}")
    print(f"dist agrees (±0.15pp)  : {entry_ok}/{total}")
    print(f"ATR stop agrees (±0.1%): {stop_ok}/{total}")
    if missing:
        print(f"skipped (no cached CSV): {missing}")
    if failures:
        print(f"\n{len(failures)} mismatch(es):")
        for f in failures[:40]:
            print("  ", *f)
        if len(failures) > 40:
            print(f"   … and {len(failures) - 40} more")
    return 0 if not failures else 1


def _engine_atr(daily: list[OHLCVBar], day) -> float | None:
    """The backtest's ATR read: calc_atr on the daily series, prior close."""
    settled = sorted(
        (b for b in daily if b.timestamp.astimezone(IST).date() < day),
        key=lambda b: b.timestamp,
    )
    if len(settled) < CFG.atr_period + 2:
        return None
    v = ind.atr(ind.bars_to_df(settled), CFG.atr_period).iloc[-1]
    return None if v != v else float(v)


if __name__ == "__main__":
    _ = timedelta  # keep the import honest for future window tweaks
    raise SystemExit(main())
