#!/usr/bin/env python
"""
Re-verify the scrip-master facts the F&O registry is built on (Decision 036).

``src/data_sources/dhan_fno.py`` encodes four measured properties of Dhan's
public scrip master. Every one of them is a claim about someone else's CSV,
and every one of them silently stops being true if that CSV changes shape.
This script re-measures all four against a freshly downloaded master and
prints a PASS/FAIL line per claim, so "no guesswork in lot sizing" stays a
property of the system rather than a note about one afternoon in August 2026.

Run it after any Dhan master change, before enabling either F&O bucket, and
whenever a contract lookup returns something surprising::

    python -m scripts.fno_registry_audit

Exits non-zero if any claim fails, so it can gate a deploy.

Reads only public data — no token, no account, no DB.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from io import BytesIO

import httpx

from src.data_sources.dhan_fno import (
    _FNO_COLUMNS,
    _SCRIP_MASTER_URL,
    FnoRegistry,
)

# The values recorded in the module docstring, as of the measurement date.
# A drift here is not automatically a failure — the master genuinely changes
# as contracts list and expire — but a LARGE drift means re-read the source.
_MEASURED_ON = "2026-08-28"
_EXPECT_NSE_D_ROWS = 74_322
_EXPECT_AMBIGUOUS_NAMES = 462
_EXPECT_FRACTIONAL_STRIKES = 1_654
# Cash equity's known Rs 0.05 tick is the calibration point for "TICK_SIZE is
# in paise". If NSE EQ ever reads something other than 5, the unit changed.
_EQUITY_TICK_PAISE = "5.0000"

_TOLERANCE = 0.25  # accept +/-25% drift on counts before flagging


def _pct_drift(actual: int, expected: int) -> float:
    return abs(actual - expected) / expected if expected else 0.0


def main() -> int:
    print(f"Dhan scrip-master audit  (baseline measured {_MEASURED_ON})")
    print(f"source: {_SCRIP_MASTER_URL}\n")

    import pandas as pd

    with httpx.Client(timeout=120.0) as http:
        resp = http.get(_SCRIP_MASTER_URL)
        resp.raise_for_status()
        payload = resp.content
    print(f"downloaded {len(payload) / 1e6:.1f} MB")

    # Deliberately a full frame here, not the registry's chunked read: this is
    # a laptop-side audit, and cross-row uniqueness checks need every row at
    # once. The registry itself never does this — see its docstring property 4.
    frame = pd.read_csv(
        BytesIO(payload),
        dtype=str,
        usecols=[*_FNO_COLUMNS, "SYMBOL_NAME", "SERIES"],
        low_memory=False,
    )
    nse_d = frame[(frame.SEGMENT == "D") & (frame.EXCH_ID == "NSE")]
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures.append(name)

    print(f"\ntotal rows: {len(frame):,} | NSE derivatives: {len(nse_d):,}")

    # ── 1. SYMBOL_NAME is ambiguous; our key tuple is not ───────────────
    print("\n1. Symbol uniqueness")
    counts = nse_d.SYMBOL_NAME.value_counts()
    ambiguous = int((counts > 1).sum())
    check(
        "SYMBOL_NAME is still ambiguous",
        ambiguous > 0,
        f"{ambiguous} names cover {int(counts[counts > 1].sum())} contracts "
        f"(baseline {_EXPECT_AMBIGUOUS_NAMES}) — this is WHY we mint our own",
    )
    key = nse_d.groupby(
        ["UNDERLYING_SYMBOL", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE"],
        dropna=False,
    ).size()
    check(
        "(underlying, expiry, strike, type) is unique",
        bool((key <= 1).all()),
        f"max duplicates {int(key.max())} — the minted symbol's guarantee",
    )
    check(
        "SECURITY_ID is unique",
        bool(nse_d.SECURITY_ID.is_unique),
        "the format-proof reverse join for the reconciler",
    )

    # ── 2. Futures sentinels ────────────────────────────────────────────
    print("\n2. Futures sentinels")
    futures = nse_d[nse_d.INSTRUMENT.isin(["FUTIDX", "FUTSTK"])]
    opt_types = set(futures.OPTION_TYPE.dropna().unique())
    check(
        "futures OPTION_TYPE is the 'XX' sentinel",
        opt_types <= {"XX"},
        f"observed {sorted(opt_types)}",
    )
    fut_strikes = set(futures.STRIKE_PRICE.dropna().unique())
    check(
        "futures STRIKE_PRICE is a non-positive sentinel",
        all(Decimal(s) <= 0 for s in fut_strikes),
        f"observed {sorted(fut_strikes)[:3]}",
    )

    # ── 3. Fractional strikes ───────────────────────────────────────────
    print("\n3. Strike precision")
    strikes = pd.to_numeric(nse_d.STRIKE_PRICE, errors="coerce")
    fractional = int(((strikes % 1 != 0) & (strikes > 0)).sum())
    check(
        "fractional strikes exist (an int cast would collide them)",
        fractional > 0,
        f"{fractional:,} half-point strikes (baseline "
        f"{_EXPECT_FRACTIONAL_STRIKES:,})",
    )

    # ── 4. Tick size units, and the per-contract spread ─────────────────
    print("\n4. Tick size")
    eq = frame[
        (frame.SEGMENT == "E") & (frame.EXCH_ID == "NSE") & (frame.SERIES == "EQ")
    ]
    check(
        "TICK_SIZE is in paise",
        _EQUITY_TICK_PAISE in set(eq.TICK_SIZE.dropna().unique()),
        f"NSE cash equity's known Rs 0.05 tick reads as {_EQUITY_TICK_PAISE}",
    )
    coarse = nse_d[pd.to_numeric(nse_d.TICK_SIZE, errors="coerce") > 5]
    check(
        "contracts tick coarser than Rs 0.05 exist",
        len(coarse) > 0,
        f"{len(coarse)} contracts — the hardcoded Rs 0.05 snap would be "
        "refused off-tick on every one of them",
    )

    # ── 5. Size drift ───────────────────────────────────────────────────
    print("\n5. Segment size")
    drift = _pct_drift(len(nse_d), _EXPECT_NSE_D_ROWS)
    check(
        "NSE derivative row count is in range",
        drift <= _TOLERANCE,
        f"{len(nse_d):,} vs baseline {_EXPECT_NSE_D_ROWS:,} ({drift:.0%} drift)",
    )

    # ── 6. End-to-end: the registry actually parses this master ─────────
    print("\n6. Registry round-trip (index underlyings)")
    index_names = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    reg = FnoRegistry(underlyings=index_names)
    reg.refresh()
    contracts = reg.contracts
    check(
        "registry loads the index universe",
        len(contracts) > 0,
        f"{len(contracts):,} live contracts across "
        f"{len(reg.underlyings())} underlyings",
    )
    today = date.today()
    check(
        "no expired contract survives the parse",
        all(c.expiry >= today for c in contracts),
        "an expired contract resolves, so an order could be built on it",
    )
    for name in sorted(index_names & reg.underlyings()):
        futs = reg.futures(name)
        if not futs:
            continue
        front = futs[0]
        print(
            f"       {name:11s} lot={front.lot_size:>5d} "
            f"tick=Rs {front.tick_size} freeze={front.freeze_qty:>6d} "
            f"front={front.expiry} expiries={len(reg.expiries(name))}"
        )

    print(
        f"\n{'AUDIT FAILED: ' + ', '.join(failures) if failures else 'AUDIT PASSED'}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
