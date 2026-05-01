#!/usr/bin/env python
"""
Fetch perpetual symbols from Binance + Delta, generate the symbol
mapping CSV, and optionally load into the DB.

Usage::

    python scripts/sync_symbols.py                 # generate CSV only
    python scripts/sync_symbols.py --load-db       # also upsert into DB
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parent.parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.data_sources.symbol_loader import (  # noqa: E402
    fetch_mappings,
    load_to_db,
    save_csv,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync symbol mappings")
    parser.add_argument(
        "--load-db",
        action="store_true",
        help="Also upsert into the symbol_mapping DB table",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Override CSV output path",
    )
    args = parser.parse_args()

    print("Fetching perpetual symbols from Binance + Delta …")
    rows = fetch_mappings()

    both = [r for r in rows if r["listed_on_binance"] and r["listed_on_delta"]]
    b_only = sum(1 for r in rows if r["listed_on_binance"] and not r["listed_on_delta"])
    d_only = sum(1 for r in rows if r["listed_on_delta"] and not r["listed_on_binance"])
    print(f"\nBinance-only: {b_only}")
    print(f"Delta-only  : {d_only}")
    print(f"Both        : {len(both)}")
    print(f"Total       : {len(rows)}")

    print("\nTop overlapping symbols:")
    for r in both[:20]:
        canon = r["canonical_symbol"]
        bsym = r["binance_symbol"]
        dsym = r["delta_symbol"]
        print(f"  {canon:>10}  Binance={bsym:<16} Delta={dsym}")

    csv_path = save_csv(rows, args.csv) if args.csv else save_csv(rows)
    print(f"\nCSV saved to {csv_path}")

    if args.load_db:
        print("\nUpserting into symbol_mapping table …")
        count = load_to_db(rows)
        print(f"  {count} rows upserted")

    print("\nDone.")


if __name__ == "__main__":
    main()
