#!/usr/bin/env python
"""
Validate the fee rate card against REAL billed charges (Decision 036, Phase C).

The card in ``fee_rates.yaml`` is a set of claims about someone else's price
list. Signing it on assertion would be exactly the guesswork it exists to
remove — so this replays it against charges Dhan has already billed.

That is possible today, before either F&O bucket exists, because swing-indian
and intraday-indian have been trading cash equity on this same account for
months and the reconciler has been stamping real per-order charges onto
``Trade.extra["charges"]`` since 2026-08. The cash-equity segments can
therefore be checked against actual contract notes, and a card whose equity
lines reconcile is a card whose F&O lines were assembled the same careful way.

READ-ONLY. It runs SELECTs and prints; it writes nothing, places nothing, and
never calls the Dhan API — a second session would evict the bot's token.

    python -m scripts.fee_card_reconcile              # last 90 days
    python -m scripts.fee_card_reconcile --days 30
    python -m scripts.fee_card_reconcile --tolerance 0.05

Exits non-zero if any segment drifts past the tolerance, so it can gate a
sign-off. It does NOT require the card to be signed — checking an unsigned
card is the entire point.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import BrokerName, OrderStatus, Trade
from src.shared.contracts import is_derivative, parse_contract_symbol
from src.shared.costs import (
    FeeRateCard,
    drift_ratio,
    estimate_charges,
    load_fee_card,
)

# Cash-equity product -> card segment. Charges differ per PRODUCT, not per
# bucket, which is why this keys on what the order was actually sent as.
_PRODUCT_SEGMENT = {
    "CNC": "nse_equity_delivery",
    "INTRADAY": "nse_equity_intraday",
    # MTF is NOT carded: its brokerage follows intraday but its STT follows
    # delivery, and that blend was never sourced. Skipped rather than guessed.
    "MTF": None,
    # MCX carry-forward. Dhan's product for a commodity futures position.
    "MARGIN": "mcx_futures",
    "NRML": "mcx_futures",
}

# Fallback when the order predates product recording (Decision 036 added it).
# Deliberately conservative — the Decision 031 CNC fallback means an
# intraday-indian order may have gone as CNC, so a bucket-derived guess is
# exactly wrong in the case a cost check most needs to get right.
_BUCKET_SEGMENT_GUESS = {
    "intraday-indian": "nse_equity_intraday",
    "swing-indian": None,
}

_LINES = ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst")


def _segment_for(trade: Trade) -> tuple[str | None, bool]:
    """``(segment, is_guess)`` for one trade.

    ``is_guess`` is True when the product was not recorded and the segment had
    to be inferred from the bucket. A guessed row still reconciles, but its
    drift cannot be read as a statement about the RATES — it may equally be a
    statement about the attribution, and the summary says so.
    """
    if is_derivative(trade.symbol):
        key = parse_contract_symbol(trade.symbol)
        if key is None:  # pragma: no cover — is_derivative already proved it
            return None, False
        return ("nse_fno_options" if key.option_type else "nse_fno_futures"), False
    product = str((trade.extra or {}).get("product") or "").upper()
    if product:
        return _PRODUCT_SEGMENT.get(product), False
    return _BUCKET_SEGMENT_GUESS.get(trade.bucket_id or "", None), True


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _fill_price(trade: Trade) -> Decimal | None:
    """The price charges were actually computed on.

    ``avg_fill_price`` when the reconciler has stamped it — charges are levied
    on what filled, not on what was asked for.
    """
    extra = trade.extra or {}
    return _dec(extra.get("avg_fill_price")) or _dec(trade.price)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument(
        "--detail",
        action="store_true",
        help="print every reconciled order, not just segment totals",
    )
    ap.add_argument(
        "--tolerance",
        type=Decimal,
        default=Decimal("0.05"),
        help="fractional drift allowed on a segment's TOTAL before failing",
    )
    args = ap.parse_args()

    card: FeeRateCard = load_fee_card()
    print("Fee rate card reconciliation")
    print(f"  signed_off: {card.signed_off}")
    print(f"  segments:   {', '.join(sorted(card.segments))}")
    print(f"  window:     last {args.days} days\n")

    # Sign-off gates the TRADING path, not this check — an unsigned card is
    # exactly what we are here to examine. Estimate against a signed copy.
    working = card.model_copy(update={"signed_off": True})

    cutoff = datetime.now(tz=UTC) - timedelta(days=args.days)
    with session_scope() as session:
        trades = list(
            session.execute(
                select(Trade).where(
                    Trade.broker == BrokerName.DHAN,
                    Trade.status.in_([OrderStatus.FILLED, OrderStatus.PARTIAL]),
                    Trade.created_at > cutoff,
                )
            ).scalars()
        )

    est_by_seg: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: dict.fromkeys(_LINES, Decimal("0"))
    )
    act_by_seg: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: dict.fromkeys(_LINES, Decimal("0"))
    )
    counts: dict[str, int] = defaultdict(int)
    guessed_by_seg: dict[str, int] = defaultdict(int)
    skipped: dict[str, int] = defaultdict(int)
    rows: list[tuple] = []

    for t in trades:
        extra = t.extra or {}
        if not extra.get("charges_final"):
            skipped["charges not yet billed"] += 1
            continue
        actual = extra.get("charges")
        if not isinstance(actual, dict):
            skipped["no charge breakdown"] += 1
            continue
        segment, guessed = _segment_for(t)
        if segment is None:
            skipped["segment not carded (MTF / unknown product)"] += 1
            continue
        if guessed:
            guessed_by_seg[segment] += 1
        if segment not in card.segments:
            skipped[f"segment {segment} absent from card"] += 1
            continue
        price = _fill_price(t)
        if price is None or price <= 0 or not t.quantity:
            skipped["no usable fill price"] += 1
            continue

        est = estimate_charges(
            working,
            segment=segment,
            side=t.side.value,
            quantity=t.quantity,
            price=price,
        )
        counts[segment] += 1
        act_total = Decimal("0")
        for line in _LINES:
            est_by_seg[segment][line] += getattr(est, line)
            a = _dec(actual.get(line)) or Decimal("0")
            act_by_seg[segment][line] += a
            act_total += a
        rows.append(
            (t.created_at, t.bucket_id or "-", t.symbol, t.side.value,
             segment, guessed, t.quantity * price, est.total, act_total)
        )

    print(f"trades in window: {len(trades)}")
    for reason, n in sorted(skipped.items()):
        print(f"  skipped {n:>4}  ({reason})")
    if not counts:
        print(
            "\nNothing to reconcile. This is the expected result until the "
            "reconciler has stamped charges on some filled Dhan trades.\n"
            "The card cannot be validated from evidence yet — say so when "
            "deciding whether to sign it."
        )
        return 0

    if args.detail:
        print(f"\n{'when':<17}{'bucket':<17}{'symbol':<13}{'side':<6}"
              f"{'segment':<21}{'turnover':>12}{'est':>9}{'actual':>9}")
        for when, bucket, sym, side, seg, guess, turnover, est_t, act_t in rows:
            mark = "?" if guess else " "
            print(f"{str(when)[:16]:<17}{bucket:<17}{sym:<13}{side:<6}"
                  f"{seg + mark:<21}{turnover:>12.0f}{est_t:>9.2f}{act_t:>9.2f}")
        print("  ? = product not recorded on the order; segment inferred "
              "from the bucket")

    failures: list[str] = []
    for segment in sorted(counts):
        est = est_by_seg[segment]
        act = act_by_seg[segment]
        est_total = sum(est.values())
        act_total = sum(act.values())
        guesses = guessed_by_seg.get(segment, 0)
        note = f", {guesses} with GUESSED attribution" if guesses else ""
        print(f"\n{segment}  ({counts[segment]} orders{note})")
        print(f"  {'line':<14}{'estimated':>12}{'actual':>12}{'drift':>10}")
        for line in _LINES:
            d = drift_ratio(est[line], act[line])
            shown = "n/a" if d is None else f"{d:.1%}"
            print(f"  {line:<14}{est[line]:>12.2f}{act[line]:>12.2f}{shown:>10}")
        total_drift = drift_ratio(est_total, act_total)
        shown = "n/a" if total_drift is None else f"{total_drift:.1%}"
        print(f"  {'TOTAL':<14}{est_total:>12.2f}{act_total:>12.2f}{shown:>10}")
        if total_drift is not None and total_drift > args.tolerance:
            failures.append(f"{segment} ({shown})")
        if guesses:
            print(
                f"  NOTE: {guesses} of these orders had no recorded product, "
                "so a drift here may be mis-attribution rather than a wrong "
                "rate. Orders placed from now on record it."
            )

    if failures:
        print(
            f"\nDRIFT BEYOND {args.tolerance:.0%}: {', '.join(failures)}\n"
            "Do not sign the card until this is explained. A per-line drift "
            "usually names the wrong rate directly."
        )
        return 1
    print(
        f"\nAll reconciled segments within {args.tolerance:.0%}. "
        "Note this validates only the segments with billed trades — the F&O "
        "lines stay unvalidated until those buckets trade."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
