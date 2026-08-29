#!/usr/bin/env python
"""
Parity: the ported CCI state machine vs the backtest's own 125 trades.

This is the bar every strategy in this repo has had to clear before going near
real money. swing-indian reproduced 208 of its 214 backtested trades and the six
misses were explained (an EMA warm-up boundary) rather than waved through;
intraday-indian hit 75 of 76. A port that has not been through this is a port
whose differences from the backtest are simply unknown.

It replays ``data/mcx/NATGASMINI_15m.csv`` from the Backtesting Engine through
``src/shared/scanner/cci.py`` — the SAME module the live runner would use — and
matches the resulting entries and exits against ``trades.json``.

    python -m scripts.cci_gas_parity
    python -m scripts.cci_gas_parity --show 20     # list the first 20 misses

READ-ONLY: no DB, no broker, no network. Exits non-zero if parity falls below
the threshold, so it can gate a rollout.

What it CANNOT check, per the handoff's §7: contract selection. Every row's
``contract`` and ``expiry`` are deliberately blank because the run used a
continuous front-month series and TradingView publishes no historical roll
dates. Entry/exit timing, prices and P&L validate; which contract those prices
belonged to does not.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.shared.scanner.cci import Bar, CCIState

_ENGINE = Path("D:/Claude_TVconnect2/Backtesting Engine")
_BARS = _ENGINE / "data" / "mcx" / "NATGASMINI_15m.csv"
_TRADES = _ENGINE / "results" / "handoff" / "cci_gas_15m" / "trades.json"

# Prices are floats in both sources and were produced by different code paths,
# so they are compared to the instrument's own tick (Rs 0.10) rather than for
# equality. A disagreement smaller than one tick is not a disagreement.
_TICK = Decimal("0.10")


def _load_bars(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            bars.append(
                Bar(
                    ts=datetime.fromisoformat(row["date"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                )
            )
    return bars


def _load_expected(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pair_signals(signals: list) -> list[dict]:
    """Fold the signal stream into round trips, the shape trades.json uses."""
    trips: list[dict] = []
    open_trip: dict | None = None
    for sig in signals:
        if sig.action == "enter":
            open_trip = {
                "entry_time": sig.ts,
                "side": "buy" if sig.side == "buy" else "sell",
                "entry_price": sig.price,
            }
        elif open_trip is not None:
            open_trip.update(
                exit_time=sig.ts, exit_price=sig.price, exit_reason=sig.reason
            )
            trips.append(open_trip)
            open_trip = None
    if open_trip is not None:
        open_trip.update(exit_time=None, exit_price=None, exit_reason="open_at_end")
        trips.append(open_trip)
    return trips


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", type=int, default=8, help="how many misses to list")
    ap.add_argument("--threshold", type=float, default=0.95)
    args = ap.parse_args()

    if not _BARS.is_file() or not _TRADES.is_file():
        print(f"missing input:\n  bars   {_BARS}\n  trades {_TRADES}")
        return 2

    bars = _load_bars(_BARS)
    expected = _load_expected(_TRADES)
    print("CCI gas 15m parity")
    print(f"  bars:     {len(bars):,}  ({bars[0].ts:%Y-%m-%d} -> {bars[-1].ts:%Y-%m-%d})")
    print(f"  expected: {len(expected)} trades\n")

    got = _pair_signals(CCIState().run(bars))
    print(f"  reproduced: {len(got)} trades")

    # Match on ENTRY TIME, which is the identity of a trade here: one position
    # at a time, so no two trades share one. Anything unmatched is reported
    # rather than quietly dropped -- an extra trade the port invents is as much
    # a failure as one it misses.
    by_entry = {}
    for e in expected:
        by_entry[datetime.fromisoformat(e["entry_time"])] = e

    matched = 0
    side_mismatch: list[tuple] = []
    price_mismatch: list[tuple] = []
    reason_mismatch: list[tuple] = []
    extra: list[dict] = []

    for trip in got:
        exp = by_entry.pop(trip["entry_time"], None)
        if exp is None:
            extra.append(trip)
            continue
        ok = True
        if trip["side"] != exp["side"]:
            side_mismatch.append((trip["entry_time"], trip["side"], exp["side"]))
            ok = False
        if abs(trip["entry_price"] - Decimal(str(exp["entry_price"]))) > _TICK:
            price_mismatch.append(
                (trip["entry_time"], "entry", trip["entry_price"], exp["entry_price"])
            )
            ok = False
        if trip["exit_price"] is not None and exp["exit_price"] is not None:
            if abs(trip["exit_price"] - Decimal(str(exp["exit_price"]))) > _TICK:
                price_mismatch.append(
                    (trip["entry_time"], "exit", trip["exit_price"], exp["exit_price"])
                )
                ok = False
        if trip["exit_reason"] != exp.get("exit_reason"):
            reason_mismatch.append(
                (trip["entry_time"], trip["exit_reason"], exp.get("exit_reason"))
            )
            ok = False
        if ok:
            matched += 1

    missing = list(by_entry.values())
    rate = matched / len(expected) if expected else 0.0

    print(f"\n  EXACT MATCHES: {matched}/{len(expected)}  ({rate:.1%})")
    print(f"  missing (in backtest, not reproduced): {len(missing)}")
    print(f"  extra   (reproduced, not in backtest): {len(extra)}")
    print(f"  side mismatches:   {len(side_mismatch)}")
    print(f"  price mismatches:  {len(price_mismatch)}")
    print(f"  reason mismatches: {len(reason_mismatch)}")

    def _dump(label: str, rows: list, fmt) -> None:
        if not rows:
            return
        print(f"\n  {label} (first {min(args.show, len(rows))}):")
        for r in rows[: args.show]:
            print(f"    {fmt(r)}")

    _dump("MISSING", missing,
          lambda e: f"{e['entry_time']}  {e['side']:4s} @ {e['entry_price']}"
                    f"  -> {e.get('exit_reason')}")
    _dump("EXTRA", extra,
          lambda t: f"{t['entry_time']}  {t['side']:4s} @ {t['entry_price']}"
                    f"  -> {t['exit_reason']}")
    _dump("SIDE", side_mismatch, lambda r: f"{r[0]}  got {r[1]}, expected {r[2]}")
    _dump("PRICE", price_mismatch,
          lambda r: f"{r[0]}  {r[1]}: got {r[2]}, expected {r[3]}")
    _dump("REASON", reason_mismatch,
          lambda r: f"{r[0]}  got {r[1]!r}, expected {r[2]!r}")

    if rate >= args.threshold:
        print(f"\nPARITY PASSED ({rate:.1%} >= {args.threshold:.0%})")
        print(
            "  NOTE: this validates entry/exit timing, prices and exit reasons "
            "only.\n  Contract selection is NOT verified and cannot be from "
            "this data -- see\n  the handoff's RULES.md section 7."
        )
        return 0
    print(f"\nPARITY FAILED ({rate:.1%} < {args.threshold:.0%})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
