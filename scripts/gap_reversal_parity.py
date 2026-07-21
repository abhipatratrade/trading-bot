"""Parity check: does this repo's port reproduce the frozen gap-reversal backtest?

Re-runs the ported morning screen (``shared/scanner/gap_reversal``) and the
ported candlestick math (``shared/scanner/patterns``) over the SAME cached
5m/1D CSVs the Backtesting Engine used, for every trade in the frozen
full-window run, and compares pass/fail, pattern name, and entry bar time.

    py -3.14 scripts/gap_reversal_parity.py

Expected: 75/76 on all three axes. The one accepted miss is VEDL 2025-08-26,
whose daily history is retroactively rescaled x0.374 by the later Vedanta
demerger — see Decision 029 for why the live corporate-action guard is
formulated the way it is, and why skipping is the safe direction.

NOT a unit test: it reads the sibling Backtesting Engine repo, which CI does
not have. Re-run it by hand after any change to the pattern math, the screen,
or the frozen config.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"D:\Claude_TVconnect2\trading-bot")

from src.data_sources.base import OHLCVBar  # noqa: E402
from src.shared.scanner import indicators as ind  # noqa: E402
from src.shared.scanner.gap_reversal import (  # noqa: E402
    GapReversalConfig,
    gap_screen,
    ist_date,
    ist_time,
)
from src.shared.scanner.patterns import pattern_flags  # noqa: E402

ENGINE = Path(r"D:\Claude_TVconnect2\Backtesting Engine")
# Entry window in bar-OPEN terms: a candle "closing at 09:30" is stamped 09:25,
# and the last actionable pattern bar is 10:25 (its entry bar opens 10:30).
_PATTERN_FIRST_BAR = time(9, 25)
_PATTERN_LAST_BAR = time(10, 25)
IST = timezone(timedelta(hours=5, minutes=30))

CFG = GapReversalConfig(
    universe_size=5,
    symbols=(),
    gap_min_pct=Decimal("3.0"),
    gap_max_pct=Decimal("12.0"),
    gap_mismatch_pct=Decimal("1.0"),
    first15_body_atr_frac=Decimal("0.25"),
    atr_period=14,
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


def first_pattern(bars: list[OHLCVBar], day) -> tuple[str, str] | None:
    """(pattern_name, entry_bar_hhmm) for the first hit in the entry window."""
    ordered = sorted(bars, key=lambda b: b.timestamp)
    today_idx = [i for i, b in enumerate(ordered) if ist_date(b) == day]
    if len(today_idx) < 3:
        return None
    # Flags over the FULL series — body_avg is a 14-EMA that must not restart
    # at the session boundary (Decision 029 / patterns.py caller contract).
    flags = pattern_flags(ind.bars_to_df(ordered))
    today = [ordered[i] for i in today_idx]
    for local, i in enumerate(today_idx):
        t = ist_time(ordered[i])
        if t < _PATTERN_FIRST_BAR:
            continue
        if t > _PATTERN_LAST_BAR:
            return None
        for name in ("engulfing_bull", "hammer"):
            if bool(flags[name].iloc[i]):
                if local + 1 >= len(today):
                    return None
                return name, ist_time(today[local + 1]).strftime("%H:%M")
    return None


def main() -> int:
    runs = json.load(
        open(
            ENGINE
            / "results/scanners/nifty100_gap_reversal_opt_opt_full_20260719_030225.json"
        )
    )
    trades = runs["trades"]
    print(f"Backtest trades to reproduce: {len(trades)}\n")

    screen_ok = pat_ok = entry_ok = 0
    missing_data = 0
    failures = []

    for t in trades:
        sym, dstr = t["symbol"], t["date"]
        day = datetime.strptime(dstr, "%Y-%m-%d").date()
        i5, d1 = load(sym, "5m"), load(sym, "1D")
        if i5 is None or d1 is None:
            missing_data += 1
            continue

        cand = gap_screen(sym, i5, d1, day, CFG)
        if cand is None:
            failures.append((sym, dstr, "SCREEN REJECTED", t.get("gap_pct")))
            continue
        screen_ok += 1

        got = first_pattern(i5, day)
        if got is None:
            failures.append((sym, dstr, "NO PATTERN", t.get("pattern")))
            continue
        name, entry_hhmm = got
        if name == t["pattern"]:
            pat_ok += 1
        else:
            failures.append((sym, dstr, f"PATTERN {name} != {t['pattern']}", ""))
        if entry_hhmm == t["entry_time"]:
            entry_ok += 1
        else:
            failures.append(
                (sym, dstr, f"ENTRY {entry_hhmm} != {t['entry_time']}", "")
            )

    n = len(trades) - missing_data
    print(f"usable trades (data present): {n}  (missing data: {missing_data})")
    print(f"gap screen reproduced : {screen_ok}/{n}")
    print(f"pattern name matched  : {pat_ok}/{n}")
    print(f"entry bar time matched: {entry_ok}/{n}\n")
    if failures:
        print(f"--- {len(failures)} mismatches ---")
        for f in failures[:25]:
            print("  ", f)
    return 0 if (screen_ok == n and pat_ok == n and entry_ok == n) else 1


if __name__ == "__main__":
    sys.exit(main())
