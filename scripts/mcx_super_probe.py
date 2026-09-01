#!/usr/bin/env python
"""
Does Dhan accept a SUPER ORDER on MCX with a MARGIN product? (Decision 034/037)

The question, and why it is worth a real order. A Super Order carries the entry
and its protective stop in ONE request, so a stop the venue refuses means the
ENTRY NEVER HAPPENS — fail-safe. The alternative, and what commodity-indian
uses today, is a plain entry followed by a separate stop from the Decision 022
sweep. That is fail-OPEN, and on 2026-09-01 it failed exactly that way: two
NATGASMINI lots were bought and neither ever got a stop, because the position
could not be matched to the ledger and read as somebody else's.

`attached_stops: false` in buckets.yaml says only "unproven on MCX". Nobody
tested it; the older path was simply assumed safer. This settles it.

NOTE this is a DIFFERENT endpoint from the Forever Order probe. That one
(``/v2/forever/orders``, proven accepted on MCX_COMM with MARGIN on
2026-08-31) rests a STANDALONE long-dated trigger. This one
(``/v2/super/orders``) attaches a stop to an entry. Proving one says nothing
about the other.

    python -m scripts.mcx_super_probe                 # DRY RUN, places nothing
    python -m scripts.mcx_super_probe --place         # places, then cancels

SAFETY — and read this, because it is WEAKER than the forever probe's:

* **This order CAN FILL.** The forever probe parked a trigger 50% from market
  where it could never fire. A super order's entry is a real entry, and its
  legs must sit inside the scrip's daily circuit band or Dhan rejects the whole
  request for a reason that has nothing to do with MCX support. So the entry
  rests only ``--entry-pct`` BELOW market (default 3%) — close enough to stay
  inside the band, far enough that filling needs a 3% adverse move in the
  couple of seconds before the cancel. Unlikely, not impossible.
* Quantity is ONE LOT (Dhan's MCX quantity is in LOTS, so 1, not 250). If it
  does fill, that is ~Rs 27,800 of margin and a position you must square off.
* Dry run is the default; ``--place`` is required to send anything.
* A live price is REQUIRED before placing — every distance below is relative to
  market, so a missing price means no guarantee at all.
* Cancellation is in a ``finally``, and a failed cancel shouts.
* Reads the bot's token from the shared ``dhan_token`` row via a STATIC manager
  with no mint path, so it cannot evict the live session.

A rejection is a RESULT, not a failure — but only if it names the PAYLOAD. A
DH-905 "Invalid IP" from an unlisted host, a quota error, or a circuit-band
complaint say nothing about whether MCX takes a super order, and this script
reports those as INCONCLUSIVE rather than pretending otherwise. That
distinction was learned the hard way on 2026-08-31, when the forever probe
printed a confident NOT-AVAILABLE verdict on an IP block.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from sqlalchemy import select

from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanAPIError, DhanClient
from src.core.db import session_scope
from src.core.models import DhanToken
from src.data_sources.dhan_fno import FnoRegistry

_SUPER_PATH = "/v2/super/orders"
_ENTRY_LEG = "ENTRY_LEG"

# Rejections that say NOTHING about whether MCX takes a super order.
#
# Two families, both fatal to the question rather than answers to it:
# the request never reached Dhan's validation (IP allowlist, auth, quota), or
# it reached it and was refused on PRICE GEOMETRY — a leg outside the daily
# circuit band, an inverted target/stop. The second is a property of the
# numbers this script chose, not of the venue's support, and retrying with a
# tighter --entry-pct is the fix.
_PERIMETER_MARKERS = (
    "invalid ip",
    "invalid token",
    "invalid client",
    "unauthor",
    "forbidden",
    "too many requests",
    "rate limit",
    "timed out",
    "timeout",
    "internal server",
    "service unavailable",
    "bad gateway",
)
_GEOMETRY_MARKERS = (
    "circuit",
    "price band",
    "out of range",
    "range",
    "dpr",
    "freeze",
    "market closed",
    "market is closed",
    "trading is not allowed",
    "session",
)

# Only a refusal that names the ORDER KIND, product, or segment answers it.
_VERDICT_MARKERS = (
    "product",
    "segment",
    "super",
    "not allowed",
    "not supported",
    "not permitted",
    "bo/co",
    "bracket",
)


def classify(message: str) -> str:
    """'verdict' | 'geometry' | 'perimeter' — biased hard AWAY from 'verdict'.

    A wrong 'inconclusive' costs one more probe run. A wrong 'not available'
    retires a live safety question on evidence nobody collected, which is
    exactly what happened on 2026-08-31.
    """
    text = message.lower()
    if any(m in text for m in _PERIMETER_MARKERS):
        return "perimeter"
    if any(m in text for m in _GEOMETRY_MARKERS):
        return "geometry"
    if any(m in text for m in _VERDICT_MARKERS):
        return "verdict"
    return "perimeter"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", default="NATGASMINI")
    ap.add_argument("--product", default="MARGIN", help="the value under test")
    ap.add_argument(
        "--place", action="store_true",
        help="ACTUALLY place the super order (then cancel it). Default: dry run.",
    )
    ap.add_argument(
        "--ltp", type=Decimal, default=None,
        help="last traded price; required with --place if the quote fails",
    )
    ap.add_argument(
        "--entry-pct", type=Decimal, default=Decimal("3"),
        help="how far BELOW market to rest the BUY entry, in percent",
    )
    ap.add_argument(
        "--leg-pct", type=Decimal, default=Decimal("1"),
        help="stop/target distance from the ENTRY price, in percent",
    )
    ap.add_argument("--base-url", default="https://api.dhan.co")
    args = ap.parse_args()

    with session_scope() as session:
        row = session.execute(
            select(DhanToken.client_id, DhanToken.token)
            .order_by(DhanToken.updated_at.desc())
            .limit(1)
        ).first()
    if row is None or not row[1]:
        print("no usable token in dhan_token; refusing to mint (single-session)")
        return 2
    client_id, token = row

    registry = FnoRegistry(underlyings={args.underlying}, exchange="MCX")
    futures = registry.futures(args.underlying)
    if not futures:
        print(f"no live futures for {args.underlying}")
        return 2
    front = futures[0]

    print("MCX Super Order probe")
    print(f"  mode:      {'LIVE PLACEMENT' if args.place else 'DRY RUN'}")
    print(f"  contract:  {front.symbol}  (security_id {front.security_id})")
    print(f"  segment:   {front.exchange_segment}")
    print(f"  product:   {args.product}   <- the value under test")
    print("  quantity:  1 lot  (Dhan MCX quantity is in LOTS, not units)")
    print("  endpoint:  /v2/super/orders   (NOT /v2/forever/orders)")

    client = DhanClient(
        token_manager=DhanTokenManager(static_token=token),
        client_id=client_id,
        resolve_symbol=registry.resolve,
        base_url=args.base_url,
        contract_spec=registry.spec,
    )

    ltp = args.ltp
    if ltp is None:
        try:
            quote = client._request(  # noqa: SLF001 — probe, not a code path
                "POST",
                "/v2/marketfeed/ltp",
                {front.exchange_segment: [int(front.security_id)]},
            )
            raw = (
                (quote or {})
                .get("data", {})
                .get(front.exchange_segment, {})
                .get(str(front.security_id), {})
                .get("last_price")
            )
            if raw:
                ltp = Decimal(str(raw))
                print(f"  quote:     Rs {ltp}")
        except Exception as exc:  # noqa: BLE001
            print(f"  quote:     FAILED ({type(exc).__name__}: {exc})")

    if ltp is None:
        print(
            "\n  No live price. Pass --ltp <price> to proceed.\n"
            "  Every distance below is relative to market, so without it the\n"
            "  'cannot fill' guarantee does not exist."
        )
        client.close()
        return 2

    tick = front.tick_size

    def snap(value: Decimal) -> Decimal:
        return (value / tick).quantize(Decimal("1")) * tick

    hundred = Decimal("100")
    entry = snap(ltp * (hundred - args.entry_pct) / hundred)
    stop = snap(entry * (hundred - args.leg_pct) / hundred)
    target = snap(entry * (hundred + args.leg_pct) / hundred)

    entry_gap = (ltp - entry) / ltp * hundred
    print(f"\n  last price:  Rs {ltp}")
    print(f"  entry BUY:   Rs {entry}   ({entry_gap:.2f}% below market)")
    print(f"  stop leg:    Rs {stop}")
    print(f"  target leg:  Rs {target}")

    if not (stop < entry < target):
        print("\n  REFUSING: leg geometry is inverted (need stop < entry < target).")
        client.close()
        return 2
    if entry >= ltp:
        print("\n  REFUSING: a BUY limit at or above market can fill instantly.")
        client.close()
        return 2

    body = {
        "dhanClientId": client_id,
        "transactionType": "BUY",
        "exchangeSegment": front.exchange_segment,
        "productType": args.product,
        "orderType": "LIMIT",
        "securityId": front.security_id,
        "quantity": 1,
        "price": float(entry),
        "targetPrice": float(target),
        "stopLossPrice": float(stop),
        "trailingJump": 0,
        "correlationId": "super-probe",
    }
    print("\n  payload:")
    print("   ", json.dumps(body, indent=2).replace("\n", "\n    "))

    if not args.place:
        print(
            "\nDRY RUN — nothing sent.\n"
            "  Re-run with --place to create and immediately cancel it.\n"
            f"  WARNING: unlike the forever probe this CAN fill if the market\n"
            f"  drops {entry_gap:.2f}% before the cancel lands. One lot."
        )
        client.close()
        return 0

    order_id = None
    try:
        print(f"\n  POST {args.base_url}{_SUPER_PATH} ...")
        result = client._request("POST", _SUPER_PATH, body)  # noqa: SLF001
        print(f"  RESPONSE: {json.dumps(result)}")
        order_id = (result or {}).get("orderId") if isinstance(result, dict) else None
        print(
            f"\nSUPER ORDER ACCEPTED ON MCX WITH productType={args.product}.\n"
            "  The stop can be ATOMIC with the entry here: a refused stop means\n"
            "  the entry never happens. Set attached_stops: true for\n"
            "  commodity-indian and the 2026-09-01 naked-position path is gone\n"
            "  by construction, not by patch."
        )
    except DhanAPIError as exc:
        verdict = classify(str(exc))
        print(f"  REJECTED: [{exc.code}] {exc}")
        if verdict == "verdict":
            print(
                "\nNOT AVAILABLE — the message above names the payload.\n"
                "  attached_stops must stay false on MCX, and the standalone\n"
                "  sweep remains the only stop path. Its ownership matching is\n"
                "  therefore load-bearing and must never silently fail again."
            )
            return 1
        if verdict == "geometry":
            print(
                "\nINCONCLUSIVE — refused on PRICE, not on support.\n"
                "  A leg fell outside the daily band, or the session is shut.\n"
                "  Retry inside market hours with a smaller --entry-pct/--leg-pct.\n"
                "  Nothing has been learned about MCX super orders."
            )
            return 3
        print(
            "\nINCONCLUSIVE — the request never reached the product check.\n"
            "  This is about the CONNECTION, not about MCX. Re-run from an\n"
            "  allowed IP (the bot VM, 34.14.200.220 — docs/runbook.md)."
        )
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return 1
    finally:
        if order_id:
            try:
                cancelled = client._request(  # noqa: SLF001
                    "DELETE", f"{_SUPER_PATH}/{order_id}/{_ENTRY_LEG}"
                )
                print(f"  CANCELLED {order_id}: {json.dumps(cancelled)}")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"  !! CANCEL FAILED for {order_id} ({exc}).\n"
                    f"  !! A LIVE BUY ORDER MAY BE RESTING {entry_gap:.2f}% BELOW\n"
                    f"  !! MARKET. CANCEL IT BY HAND IN THE DHAN APP NOW."
                )
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
