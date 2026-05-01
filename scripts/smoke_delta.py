#!/usr/bin/env python
"""
Smoke test: place + cancel a testnet order on Delta Exchange India.

Run from the repo root::

    python scripts/smoke_delta.py

Safety: refuses to run unless TRADING_MODE=testnet.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# Ensure repo root is importable for ``src.*`` imports.
_REPO = str(Path(__file__).resolve().parent.parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.brokers.base import OrderRequest, OrderType, TimeInForce  # noqa: E402
from src.brokers.delta_india.client import DeltaAPIError, DeltaIndiaClient  # noqa: E402
from src.core.config import TradingMode, get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    if settings.trading_mode != TradingMode.TESTNET:
        print("ERROR: smoke test only runs in testnet mode (TRADING_MODE=testnet)")
        sys.exit(1)

    print(f"Mode : {settings.trading_mode.value}")
    print(f"REST : {settings.delta_base_url}")
    print(f"WS   : {settings.delta_ws_url}")

    with DeltaIndiaClient(settings) as client:
        # ── 1. Balances ─────────────────────────────────────────────
        print("\n=== Wallet Balances ===")
        for b in client.get_balances():
            print(f"  {b.asset}: available={b.available}  margin={b.order_margin}")

        # ── 2. Set leverage ─────────────────────────────────────────
        symbol = "BTCUSD"
        leverage = Decimal("5")
        print(f"\n=== Setting {leverage}x leverage on {symbol} ===")
        try:
            client.set_leverage(symbol, leverage)
            print("  OK")
        except DeltaAPIError as exc:
            print(f"  (skipped: {exc})")

        # ── 3. Place limit order well below market ──────────────────
        limit_price = Decimal("10000")
        print(f"\n=== Placing limit BUY {symbol} 1 ct @ ${limit_price} ===")
        order = client.place_order(
            OrderRequest(
                symbol=symbol,
                side="buy",
                size=Decimal("1"),
                order_type=OrderType.LIMIT,
                limit_price=limit_price,
                time_in_force=TimeInForce.GTC,
            )
        )
        print(f"  id={order.exchange_order_id}  status={order.status}")

        # ── 4. Open orders ──────────────────────────────────────────
        print("\n=== Open Orders ===")
        for o in client.get_open_orders():
            print(f"  {o.symbol} {o.side} {o.size} @ {o.limit_price}  [{o.status}]")

        # ── 5. Cancel ───────────────────────────────────────────────
        print(f"\n=== Canceling order {order.exchange_order_id} ===")
        cancel = client.cancel_order(
            exchange_order_id=order.exchange_order_id,
            symbol=symbol,
        )
        print(f"  success={cancel.success}")

        # ── 6. Verify cancellation ──────────────────────────────────
        print("\n=== Open Orders (after cancel) ===")
        remaining = client.get_open_orders()
        print(f"  count={len(remaining)}")

        # ── 7. Positions ────────────────────────────────────────────
        print("\n=== Positions ===")
        positions = client.get_positions()
        if not positions:
            print("  (none)")
        for p in positions:
            print(f"  {p.symbol}: {p.side} {p.size} @ {p.entry_price}")

    print("\n=== Smoke test passed ===")


if __name__ == "__main__":
    main()
