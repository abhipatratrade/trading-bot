#!/usr/bin/env python
"""
Does Dhan's margin calculator answer for MCX? (Decision 037, the F2 gate.)

Phase C made the margin preflight MANDATORY for a derivative: no answer means
no order, because a derivative has no 1x to fall back on. That rule is only
safe if the preflight actually answers — and `/v2/margincalculator` has never
been answered by a live Dhan account for anything, let alone for `MCX_COMM`.
Until it does, the commodity bucket will refuse every order. This is the script
that finds that out before Monday rather than during it.

    python -m scripts.mcx_margin_probe
    python -m scripts.mcx_margin_probe --price 275 --qty 1

TOKEN SAFETY — read this before running it anywhere near the live bot.

Dhan keeps ONE active token per client id, so minting evicts whoever held it.
This script therefore CONSUMES the bot's existing token from the shared
`dhan_token` Postgres row and wraps it in a STATIC manager, which has no mint
path at all. It cannot evict the bot, and if the row is empty it stops rather
than falling back to anything that could.

It places NO orders. `/v2/margincalculator` is a computation endpoint: it
prices a hypothetical order and returns rupees.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from sqlalchemy import select

from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanClient
from src.core.db import session_scope
from src.core.models import DhanToken
from src.data_sources.dhan_fno import FnoRegistry

# Dhan product codes worth trying for a commodity future. The carry-forward one
# is the whole point (the strategy holds overnight — that is where its edge
# lives), but INTRADAY is probed too so a refusal can be told from "wrong
# product string", which is one of the unverified items in Decision 036.
_PRODUCTS = ("MARGIN", "NRML", "INTRADAY", "CNC")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", default="NATGASMINI")
    ap.add_argument("--qty", type=int, default=1, help="ORDER units (lots on MCX)")
    ap.add_argument(
        "--price", type=Decimal, default=None,
        help="probe price; defaults to the live quote, then to a fallback",
    )
    ap.add_argument("--fallback-price", type=Decimal, default=Decimal("275"))
    ap.add_argument(
        "--base-url", default="https://api.dhan.co",
        help="order host; the margin calculator lives on the LIVE host",
    )
    args = ap.parse_args()

    # Both the client id AND the token come from the shared `dhan_token` row,
    # so this needs no Dhan environment variables at all. That matters: House
    # Rule 5 keeps the credentials on the VM, so a developer machine has the
    # database but not the secrets — and the row is keyed BY client id, which
    # is the one piece that would otherwise have to come from somewhere else.
    with session_scope() as session:
        row = session.execute(
            select(DhanToken.client_id, DhanToken.token)
            .order_by(DhanToken.updated_at.desc())
            .limit(1)
        ).first()

    print("MCX margin preflight probe")
    if row is None or not row[1]:
        print(
            "\n  no usable token in the dhan_token table.\n"
            "  REFUSING to mint: Dhan is single-session, so minting here would "
            "evict the bot.\n"
            "  Run this while the bot is up — it publishes the row."
        )
        return 2
    client_id, token = row
    print(f"  base url:  {args.base_url}")
    print(f"  client id: {client_id}")
    print(f"  token:     {token[:12]}... (read from dhan_token, NOT minted)")

    registry = FnoRegistry(underlyings={args.underlying}, exchange="MCX")
    contracts = registry.futures(args.underlying)
    if not contracts:
        print(f"\n  no live futures for {args.underlying} in the scrip master")
        return 2
    front = contracts[0]
    print(
        f"\n  contract:  {front.symbol}"
        f"\n    security_id {front.security_id}  segment {front.exchange_segment}"
        f"\n    lot_size {front.lot_size} (order units)  "
        f"multiplier {front.multiplier} (underlying units)"
        f"\n    expiry {front.expiry}  tick Rs {front.tick_size}"
    )

    client = DhanClient(
        token_manager=DhanTokenManager(static_token=token),
        client_id=client_id,
        resolve_symbol=registry.resolve,
        base_url=args.base_url,
        contract_spec=registry.spec,
    )

    # The probe price only has to be REALISTIC, not live: SPAN scales with
    # notional, so a price in the right neighbourhood answers the question
    # ("does it answer, and roughly what fraction?") without a quote — and
    # fetching one would be a second call on a rate-limited account.
    price = args.price or args.fallback_price
    suffix = "" if args.price else " (default; pass --price to override)"
    print(f"\n  probe price: Rs {price}{suffix}")

    notional = front.multiplier * price * args.qty
    print(f"  notional:    Rs {notional:,.2f}  ({args.qty} lot(s))")

    print("\n  asking /v2/margincalculator:")
    answered: dict[str, Decimal] = {}
    for product in _PRODUCTS:
        for side in ("BUY", "SELL"):
            try:
                got = client.required_margin(
                    front.symbol, side, Decimal(args.qty), price, product=product
                )
            except Exception as exc:  # noqa: BLE001 — a probe reports, never raises
                print(f"    {product:9s} {side:4s}  EXCEPTION {type(exc).__name__}: {exc}")
                continue
            if got is None:
                print(f"    {product:9s} {side:4s}  no answer")
                continue
            pct = got / notional * 100 if notional else Decimal("0")
            print(f"    {product:9s} {side:4s}  Rs {got:>12,.2f}   ({pct:.1f}% of notional)")
            answered[f"{product}/{side}"] = got

    client.close()

    print()
    if not answered:
        print(
            "PREFLIGHT DOES NOT ANSWER FOR MCX.\n"
            "  Phase C's rule stands: no margin answer means no order, so the\n"
            "  commodity bucket will refuse every entry. That is the guard\n"
            "  working, not a bug — but it means this bucket cannot trade until\n"
            "  either the endpoint answers or the rule is deliberately changed."
        )
        return 1

    best = min(answered.values())
    print(
        f"PREFLIGHT ANSWERS. Lowest quoted margin Rs {best:,.2f} for "
        f"{args.qty} lot(s).\n"
        f"  The backtest ASSUMED Rs 10,890/lot (15% of notional, never a broker\n"
        f"  quote). Compare: if the real figure is higher, the live book is\n"
        f"  smaller than the one that was validated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
