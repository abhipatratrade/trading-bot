"""
Build (and optionally send) an end-of-day session report by hand.

The scheduler runs this automatically at 15:45 IST on weekdays. Use this for a
backfill, a re-run after fixing data, or just to see today's report early.

    python -m scripts.eod_report                  # today (IST), store + print
    python -m scripts.eod_report --date 2026-07-28
    python -m scripts.eod_report --send           # also push the Telegram digest
    python -m scripts.eod_report --no-store       # print only, touch nothing

Reads Postgres only — never the broker. A second Dhan session would evict the
bot's token (Decision 033).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime

from src.core.alerts import send_alert
from src.core.logging import configure_logging
from src.reporting import eod
from src.shared.market_calendar import IST


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="IST session date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--send", action="store_true", help="Send the digest to Telegram."
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Do not write the session_report row.",
    )
    parser.add_argument(
        "--digest", action="store_true", help="Print the short digest, not the journal."
    )
    args = parser.parse_args()

    configure_logging()
    session_date = args.date or datetime.now(tz=IST).date()

    report = eod.gather(session_date)
    if not args.no_store:
        eod.store(report)
    print(eod.render_digest(report) if args.digest else eod.render_markdown(report))
    if args.send:
        send_alert(eod.render_digest(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
