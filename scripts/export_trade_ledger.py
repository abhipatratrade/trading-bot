"""
Write the consolidated bot-trade ledger to CSV (opens directly in Excel).

    python -m scripts.export_trade_ledger                    # every filled trade
    python -m scripts.export_trade_ledger --fy FY2026-27     # one financial year
    python -m scripts.export_trade_ledger --upload           # also mirror to Drive

Output: ``data/exports/bot_trade_ledger[_FY....].csv``, one row per FILLED
order, oldest first.

READ THIS BEFORE USING IT FOR ANYTHING THAT MATTERS. The bot records only the
orders IT placed. Trades made by hand are NOT here, and on the shared Dhan
account that is most of the activity. Charges are whatever the broker reported
at fill time -- a single ``fees`` number, not the itemised brokerage/STT/stamp/
GST breakdown an accountant works from. The complete record is the BROKER's own
tax P&L statement; this file is for reconciling against it.

Amounts are per-currency and deliberately NOT converted (see
``src/reporting/tax_ledger.py`` for why the allocator's fixed 85.0 USD/INR must
not be used here).
"""

from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from src.core.db import session_scope
from src.core.export import upload_to_gdrive
from src.core.logging import configure_logging
from src.core.models import Trade
from src.reporting.tax_ledger import COLUMNS, build_ledger, summarise

_EXPORT_DIR = Path("data/exports")


def _fmt(value: object) -> str:
    """Decimals as plain strings — no scientific notation, no float drift."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fy", default=None, help='e.g. "FY2026-27". Default: all.')
    parser.add_argument("--upload", action="store_true", help="Mirror to Drive.")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    configure_logging()

    with session_scope() as session:
        trades = list(
            session.execute(select(Trade).order_by(Trade.created_at)).scalars()
        )
    rows = build_ledger(trades, fy=args.fy)

    if not rows:
        print(f"No filled trades{f' in {args.fy}' if args.fy else ''}.")
        return 0

    suffix = f"_{args.fy}" if args.fy else ""
    path = args.out or (_EXPORT_DIR / f"bot_trade_ledger{suffix}.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        # utf-8-sig: Excel on Windows needs the BOM or it mangles the header.
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _fmt(row.get(k)) for k in COLUMNS})

    print(f"Wrote {len(rows)} filled trade(s) -> {path}")
    print(f"  {rows[0]['date_ist']} .. {rows[-1]['date_ist']}")
    print()
    print("Totals per currency (realized P&L counted on CLOSING legs only,")
    print("so it is not double-counted across the two legs of a round-trip):")
    for currency, totals in sorted(summarise(rows).items()):
        print(
            f"  {currency}: {int(totals['fills'])} fills | "
            f"fees {_fmt(totals['fees'])} | realized {_fmt(totals['realized'])}"
        )
    print()
    print("BOT TRADES ONLY. Orders placed by hand are not in this file.")
    print("Reconcile against the broker's own tax P&L statement, which is the")
    print("complete record and itemises charges. Classification is a CA's call.")

    if args.upload:
        print()
        print("uploaded to Drive" if upload_to_gdrive(path) else "UPLOAD FAILED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
