"""
Record a capital adjustment for a bucket's cumulative P&L baseline.

The dashboard P&L is  equity − (capital_inr + adjustments)  where
``adjustments`` lives in ``bucket_state.extra["capital_adjustments_inr"]``
(Decision 025). Whenever money is manually moved on the sub-account, run
this so the move doesn't read as fake P&L:

    deposit ₹X    →  --amount  X      (adjustments += X)
    withdraw ₹X   →  --amount -X      (adjustments -= X)

Or reset the baseline so P&L counts from the CURRENT wallet:

    --rebase          (sets adjustments = current equity − capital_inr,
                       i.e. cumulative P&L becomes 0 right now)

Every run writes an audit_log row.

Usage:
    python -m scripts.record_capital_adjustment longterm-crypto --rebase
    python -m scripts.record_capital_adjustment longterm-crypto \
        --amount 5000 --note "topped up testnet wallet"
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import select

from src.core.db import session_scope
from src.core.logging import configure_logging, get_logger
from src.core.models import AuditEventType, AuditLog, BucketState

_log = get_logger("scripts.capital_adjustment")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bucket_id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--amount",
        type=Decimal,
        help="INR delta: +deposit / -withdrawal (added to adjustments)",
    )
    group.add_argument(
        "--rebase",
        action="store_true",
        help="set adjustments = current equity - capital (P&L becomes 0 now)",
    )
    parser.add_argument("--note", default="", help="reason for the audit row")
    args = parser.parse_args()

    with session_scope() as session:
        state = session.execute(
            select(BucketState).where(BucketState.bucket_id == args.bucket_id)
        ).scalar_one_or_none()
        if state is None:
            raise SystemExit(f"no bucket_state row for {args.bucket_id!r}")

        extra = dict(state.extra or {})
        old = Decimal(str(extra.get("capital_adjustments_inr", "0")))
        equity = state.available_balance_inr + state.locked_margin_inr

        if args.rebase:
            new = equity - state.capital_inr
            action = "rebase"
        else:
            new = old + args.amount
            action = f"amount {args.amount:+}"

        extra["capital_adjustments_inr"] = str(new)
        state.extra = extra
        session.add(
            AuditLog(
                strategy_id=args.bucket_id,
                event_type=AuditEventType.PARAMS_LOADED,
                message=(
                    f"capital adjustment {action}: {old} -> {new} "
                    f"(equity {equity}, capital {state.capital_inr})"
                ),
                payload={
                    "bucket_id": args.bucket_id,
                    "action": action,
                    "old_adjustments_inr": str(old),
                    "new_adjustments_inr": str(new),
                    "equity_inr": str(equity),
                    "capital_inr": str(state.capital_inr),
                    "note": args.note,
                },
            )
        )
        pnl_now = equity - (state.capital_inr + new)

    _log.info(
        "capital_adjustment_recorded",
        bucket_id=args.bucket_id,
        old=str(old),
        new=str(new),
        pnl_after=str(pnl_now),
    )
    print(
        f"{args.bucket_id}: adjustments {old} -> {new} | "
        f"equity {equity} | cumulative P&L now {pnl_now}"
    )


if __name__ == "__main__":
    main()
