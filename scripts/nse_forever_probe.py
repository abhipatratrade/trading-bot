#!/usr/bin/env python
"""
Does Dhan accept a Forever Order on NSE_EQ with productType MTF? (Decision 035)

The question this settles, and why it gates a switch. swing-indian carries MTF
positions for days. Its protective stop is a DAY order, which Dhan expires at
15:30, so from the close to the next morning's sweep the book rests on nothing
— and on 2026-09-12 01:34 IST ``stop_coverage`` halted the bucket over exactly
that. Decision 035's answer is a Forever Order (GTT), which rests up to 365
days. ``scripts/mcx_forever_probe.py`` proved the mechanism on MCX_COMM with
MARGIN on 2026-08-31. NSE_EQ with MTF — the combination this bucket needs — is
what the docs list and what nobody has sent. This sends it, once, and cancels.

    python -m scripts.nse_forever_probe                    # DRY RUN, sends nothing
    python -m scripts.nse_forever_probe --place            # places, then cancels

What it proves is what the bot will send: the payload comes from
``DhanClient.forever_body_for`` — the same builder the stop sweep uses under
``forever_stops_enabled`` — not from a hand-written twin.

SAFETY, because this can put a real order on a real account:

* **Dry run is the default.** Without ``--place`` it prints the payload and
  exits.
* **It sells ONE share of a position the bot already holds** (auto-picked from
  the swing-indian ledger, or ``--symbol``). A protective SELL is the shape
  under test, and a sell of held stock is the only kind that could not open a
  short if the cancel below somehow failed.
* **The trigger is far below market and CHECKED.** No live price ⇒ refuse.
  Nearer than ``--min-distance-pct`` ⇒ refuse.
* **Cancellation is in a ``finally``.**
* **Static token.** Read from the shared ``dhan_token`` row; no mint path, so
  it cannot evict the bot's session.
* **Run it from the VM IP.** Dhan's order endpoints enforce an IP allowlist
  (DH-905 off the VM); a perimeter rejection is INCONCLUSIVE and says nothing
  about MTF.

A rejection that names the product or the segment is a RESULT: it means
Decision 035 stays closed for this bucket and the DAY stop stands.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from sqlalchemy import select

from src.brokers.base import OrderRequest, OrderType
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanAPIError, DhanClient
from src.core.db import session_scope
from src.core.models import DhanToken, Position, PositionSide
from src.data_sources.dhan import DhanData

_FOREVER_PATH = "/v2/forever/orders"

_PERIMETER_MARKERS = (
    "invalid ip", "dh-905", "dh-901", "dh-906", "token", "unauthor",
    "too many", "rate limit", "805", "timeout", "connection",
)
_VERDICT_MARKERS = (
    "mtf", "product", "not allowed", "not permitted", "invalid segment",
    "not supported", "forever", "gtt",
)


def _answers_the_question(message: str) -> bool:
    text = message.lower()
    if any(m in text for m in _PERIMETER_MARKERS):
        return False
    return any(m in text for m in _VERDICT_MARKERS)


def _held_mtf_symbol() -> str | None:
    """A long the bot holds under swing-indian — the shape under test."""
    with session_scope() as session:
        row = session.execute(
            select(Position.symbol)
            .where(
                Position.bucket_id == "swing-indian",
                Position.side == PositionSide.LONG,
                Position.quantity > 0,
            )
            .order_by(Position.symbol)
            .limit(1)
        ).first()
    return row[0] if row else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default=None, help="a held MTF long; default: auto")
    ap.add_argument("--product", default="MTF", help="the value under test")
    ap.add_argument("--place", action="store_true", help="ACTUALLY place, then cancel")
    ap.add_argument("--ltp", type=Decimal, default=None)
    ap.add_argument("--trigger-pct", type=Decimal, default=Decimal("50"))
    ap.add_argument("--min-distance-pct", type=Decimal, default=Decimal("25"))
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

    symbol = args.symbol or _held_mtf_symbol()
    if not symbol:
        print("no held swing-indian long to probe against; pass --symbol")
        return 2

    static = DhanTokenManager(static_token=token)
    data = DhanData(token_manager=static)
    security_id, segment = data.resolve(symbol)
    client = DhanClient(
        token_manager=static,
        client_id=client_id,
        resolve_symbol=data.resolve,
        base_url=args.base_url,
        product_type=args.product,
    )

    print("NSE Forever Order probe (Decision 035)")
    print(f"  mode:      {'LIVE PLACEMENT' if args.place else 'DRY RUN'}")
    print(f"  symbol:    {symbol}  (security_id {security_id}, {segment})")
    print(f"  product:   {args.product}   <- the value under test")
    print("  quantity:  1 share, SELL — the protective-stop shape")

    ltp = args.ltp
    if ltp is None:
        try:
            t = data.get_ticker(symbol)
            ltp = t.last_price or t.mark_price
            print(f"  quote:     {ltp}")
        except Exception as exc:  # noqa: BLE001
            print(f"  quote:     FAILED ({type(exc).__name__}: {exc})")
    if not ltp:
        print("\n  No live price. Pass --ltp <price>. Refusing to guess where market is.")
        client.close()
        return 2

    trigger = (ltp * (Decimal("100") - args.trigger_pct) / Decimal("100")).quantize(
        Decimal("0.05")
    )
    distance = abs(ltp - trigger) / ltp * Decimal("100")
    print(f"\n  last price:  Rs {ltp}")
    print(f"  trigger:     Rs {trigger}   ({distance:.1f}% below market)")
    if distance < args.min_distance_pct:
        print(f"\n  REFUSING: trigger is {distance:.1f}% from market, nearer than the floor.")
        client.close()
        return 2

    request = OrderRequest(
        symbol=symbol,
        side="sell",
        size=Decimal("1"),
        order_type=OrderType.MARKET,
        reduce_only=True,
        stop_price=trigger,
        product=args.product,
        client_order_id="fo-probe-nse",
        forever=True,
    )
    body = client.forever_body_for(request)
    print("\n  payload (DhanClient.forever_body_for — what the sweep would send):")
    print("   ", json.dumps(body, indent=2).replace("\n", "\n    "))

    if not args.place:
        print("\nDRY RUN — nothing sent. Re-run with --place to create and immediately cancel it.")
        client.close()
        return 0

    order_id = None
    try:
        print(f"\n  POST {args.base_url}{_FOREVER_PATH} ...")
        result = client._request("POST", _FOREVER_PATH, body)  # noqa: SLF001
        print(f"  RESPONSE: {json.dumps(result)}")
        order_id = (result or {}).get("orderId") if isinstance(result, dict) else None
        print(
            f"\nFOREVER ORDER ACCEPTED ON {segment} WITH productType={args.product}.\n"
            "  Decision 035 is available for swing-indian. Set\n"
            "  FOREVER_STOPS_ENABLED=true on the VM and restart; the sweep will\n"
            "  rest one per position from the next tick."
        )
    except DhanAPIError as exc:
        print(f"  REJECTED: [{exc.code}] {exc}")
        if _answers_the_question(str(exc)):
            print("\nNOT AVAILABLE — the message names why. The DAY stop stands.")
            return 1
        print(
            "\nINCONCLUSIVE — the request never reached the product check.\n"
            "  Re-run from an allowed IP (the bot VM, docs/runbook.md)."
        )
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return 1
    finally:
        if order_id:
            try:
                cancelled = client._request("DELETE", f"{_FOREVER_PATH}/{order_id}")  # noqa: SLF001
                print(f"  CANCELLED {order_id}: {json.dumps(cancelled)}")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"  !! CANCEL FAILED for {order_id} ({exc}). "
                    f"CANCEL IT BY HAND IN THE DHAN APP (Forever Orders tab)."
                )
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
