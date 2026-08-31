#!/usr/bin/env python
"""
Does Dhan accept a Forever Order on MCX with a MARGIN product? (Decision 035/037)

The question this settles, and why it matters. A protective stop on Dhan is sent
with ``validity: DAY`` and therefore dies at every session close. A Forever
Order lives up to 365 days. If one can be created on ``MCX_COMM`` with
``productType: MARGIN``, the commodity bucket's stop can stop expiring nightly.

The documentation does NOT answer it. Dhan's own v2 page lists creation
``productType`` as CNC / MTF; a mirror of the same docs lists CNC / MTF /
INTRADAY / MARGIN. Dhan's page is more authoritative and reads narrower, the
Annexure defines MARGIN as "Carry Forward in Futures & Options", and the retail
article frames Forever Orders around ETFs. One request settles what reading
after reading cannot.

    python -m scripts.mcx_forever_probe                 # DRY RUN, places nothing
    python -m scripts.mcx_forever_probe --place         # places, then cancels

SAFETY, because this is the one script here that can put a real order on a real
account:

* **Dry run is the default.** Without ``--place`` it prints the exact payload
  and exits. Nothing is sent.
* **A live price is REQUIRED before placing.** The trigger is set a long way
  from market, and "far from market" is only a guarantee if the market price is
  known — so a failed quote REFUSES to place rather than guessing.
* **The trigger is checked, not just computed.** If it lands within
  ``--min-distance-pct`` of the last price the script refuses.
* **Cancellation is in a ``finally``.** An exception between placement and
  cancel still cancels.
* **Quantity is ONE LOT.** Verified 2026-08-30: Dhan's MCX order quantity is in
  LOTS, so this is quantity=1, not 250.
* It reads the bot's token from the shared ``dhan_token`` row and wraps it in a
  STATIC manager with no mint path, so it cannot evict the live session.

A rejection is a RESULT, not a failure: the error message names the limitation
precisely, which is the whole point of asking.
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

_FOREVER_PATH = "/v2/forever/orders"

# Rejections that say NOTHING about whether MCX + MARGIN is permitted.
#
# The request died at the perimeter — IP allowlist, auth, quota, a malformed
# body — so Dhan never looked at ``productType`` or ``exchangeSegment`` at all.
# Reporting "not available" from one of these is how a decision gets closed on
# evidence that was never collected: on 2026-08-31 a DH-905 "Invalid IP" from
# an unlisted laptop printed the full NOT-AVAILABLE verdict, which would have
# left swing-indian's overnight gap open on a finding nobody had made.
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

# Only a refusal that names WHAT WE SENT answers the question.
_VERDICT_MARKERS = (
    "product",
    "segment",
    "order type",
    "not allowed",
    "not supported",
    "not permitted",
    "forever",
)


def _answers_the_question(message: str) -> bool:
    """True only if Dhan refused the PAYLOAD rather than the CALLER.

    Biased hard toward returning False. A wrong "inconclusive" costs one more
    probe run; a wrong "not available" closes a live risk question on nothing.
    """
    text = message.lower()
    if any(m in text for m in _PERIMETER_MARKERS):
        return False
    return any(m in text for m in _VERDICT_MARKERS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", default="NATGASMINI")
    ap.add_argument("--product", default="MARGIN", help="the value under test")
    ap.add_argument(
        "--place", action="store_true",
        help="ACTUALLY place the order (then cancel it). Default is a dry run.",
    )
    ap.add_argument(
        "--ltp", type=Decimal, default=None,
        help="last traded price; required with --place if the quote fails",
    )
    ap.add_argument(
        "--trigger-pct", type=Decimal, default=Decimal("50"),
        help="how far BELOW market to park the trigger, in percent",
    )
    ap.add_argument(
        "--min-distance-pct", type=Decimal, default=Decimal("25"),
        help="refuse to place if the trigger is nearer market than this",
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

    print("MCX Forever Order probe")
    print(f"  mode:      {'LIVE PLACEMENT' if args.place else 'DRY RUN'}")
    print(f"  contract:  {front.symbol}  (security_id {front.security_id})")
    print(f"  segment:   {front.exchange_segment}")
    print(f"  product:   {args.product}   <- the value under test")
    print("  quantity:  1 lot  (Dhan MCX quantity is in LOTS, not units)")

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
            print(f"  quote:     {json.dumps(quote)[:200]}")
            # {"data": {"MCX_COMM": {"568246": {"last_price": 276.1}}}, ...}
            raw = (
                (quote or {})
                .get("data", {})
                .get(front.exchange_segment, {})
                .get(str(front.security_id), {})
                .get("last_price")
            )
            if raw:
                ltp = Decimal(str(raw))
        except Exception as exc:  # noqa: BLE001
            print(f"  quote:     FAILED ({type(exc).__name__}: {exc})")

    if ltp is None:
        print(
            "\n  No live price. Pass --ltp <price> to proceed.\n"
            "  Refusing to compute a 'far from market' trigger without knowing\n"
            "  where the market is — that guarantee is the whole safety story."
        )
        client.close()
        return 2

    trigger = (ltp * (Decimal("100") - args.trigger_pct) / Decimal("100")).quantize(
        front.tick_size
    )
    distance = abs(ltp - trigger) / ltp * Decimal("100")
    print(f"\n  last price:  Rs {ltp}")
    print(f"  trigger:     Rs {trigger}   ({distance:.1f}% below market)")

    if distance < args.min_distance_pct:
        print(
            f"\n  REFUSING: trigger is {distance:.1f}% from market, nearer than "
            f"the {args.min_distance_pct}% floor."
        )
        client.close()
        return 2

    body = {
        "dhanClientId": client_id,
        "orderFlag": "SINGLE",
        "transactionType": "BUY",
        "exchangeSegment": front.exchange_segment,
        "productType": args.product,
        "orderType": "LIMIT",
        "validity": "DAY",
        "securityId": front.security_id,
        "quantity": 1,
        "price": float(trigger),
        "triggerPrice": float(trigger),
        "correlationId": "fo-probe",
    }
    print("\n  payload:")
    print("   ", json.dumps(body, indent=2).replace("\n", "\n    "))

    if not args.place:
        print(
            "\nDRY RUN — nothing sent.\n"
            "  Re-run with --place to actually create and immediately cancel it."
        )
        client.close()
        return 0

    order_id = None
    try:
        print(f"\n  POST {args.base_url}{_FOREVER_PATH} ...")
        result = client._request("POST", _FOREVER_PATH, body)  # noqa: SLF001
        print(f"  RESPONSE: {json.dumps(result)}")
        order_id = (result or {}).get("orderId") if isinstance(result, dict) else None
        print(
            "\nFOREVER ORDER ACCEPTED ON MCX WITH "
            f"productType={args.product}.\n"
            "  Decision 035's mechanism is available here: a stop that lives up\n"
            "  to 365 days instead of dying at session close."
        )
    except DhanAPIError as exc:
        print(f"  REJECTED: [{exc.code}] {exc}")
        if _answers_the_question(str(exc)):
            print(
                "\nNOT AVAILABLE — and the message above names why.\n"
                "  The DAY-validity stop stands, and the narrower reading of\n"
                "  the docs (CNC/MTF only) is the correct one."
            )
            return 1
        print(
            "\nINCONCLUSIVE — the request never reached the product check.\n"
            "  This rejection is about the CONNECTION, not about whether MCX\n"
            "  accepts a MARGIN Forever Order. Nothing has been learned about\n"
            "  Decision 035; do NOT close it on this. Re-run from an allowed\n"
            "  IP — the bot VM, 34.14.200.220 (docs/runbook.md)."
        )
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return 1
    finally:
        if order_id:
            try:
                cancelled = client._request(  # noqa: SLF001
                    "DELETE", f"{_FOREVER_PATH}/{order_id}"
                )
                print(f"  CANCELLED {order_id}: {json.dumps(cancelled)}")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"  !! CANCEL FAILED for {order_id} ({exc}). "
                    f"CANCEL IT BY HAND IN THE DHAN APP."
                )
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
