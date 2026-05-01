"""
Delta Exchange India WebSocket client.

Handles authentication (``key-auth``), channel subscriptions, message
dispatch via callbacks, and automatic reconnection with exponential
backoff.

Usage::

    ws = DeltaIndiaWS()
    ws.on("orders", handle_order)
    ws.on("positions", handle_position)
    await ws.connect()
    await ws.subscribe([
        {"name": "orders", "symbols": ["all"]},
        {"name": "positions", "symbols": ["all"]},
        {"name": "v2/ticker", "symbols": ["BTCUSD"]},
    ])
    await ws.listen()      # blocks until close()
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from src.core.config import Settings, get_settings
from src.core.logging import get_logger


class DeltaIndiaWS:
    """Async WebSocket client for Delta Exchange India."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._log = get_logger("brokers.delta_india.ws")
        self._api_key = self._settings.delta_api_key.get_secret_value()
        self._api_secret = self._settings.delta_api_secret.get_secret_value()
        self._ws_url = self._settings.delta_ws_url
        self._ws: Any = None
        self._callbacks: dict[str, list[Callable[[dict[str, Any]], None]]] = defaultdict(
            list
        )
        self._subscriptions: list[dict[str, Any]] = []
        self._running = False

    # ── Connection ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect and authenticate.  Re-subscribes if reconnecting."""
        self._ws = await websockets.connect(
            self._ws_url,
            ping_interval=30,
            ping_timeout=10,
        )
        await self._authenticate()
        if self._subscriptions:
            await self._send(
                {"type": "subscribe", "payload": {"channels": self._subscriptions}}
            )
        self._log.info("ws_connected", url=self._ws_url)

    async def _authenticate(self) -> None:
        ts = str(int(time.time()))
        sig = hmac.new(
            self._api_secret.encode("utf-8"),
            f"GET{ts}/live".encode(),
            hashlib.sha256,
        ).hexdigest()
        await self._send(
            {
                "type": "key-auth",
                "payload": {
                    "api-key": self._api_key,
                    "signature": sig,
                    "timestamp": ts,
                },
            }
        )
        resp = json.loads(await self._ws.recv())
        if resp.get("type") == "error":
            raise ConnectionError(f"WS auth failed: {resp}")
        self._log.info("ws_authenticated")

    # ── Subscriptions ───────────────────────────────────────────────

    async def subscribe(self, channels: list[dict[str, Any]]) -> None:
        """Subscribe to one or more channels.

        Channels are remembered so they can be re-subscribed on reconnect.
        """
        self._subscriptions.extend(channels)
        if self._ws:
            await self._send(
                {"type": "subscribe", "payload": {"channels": channels}}
            )

    def on(self, channel: str, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for messages whose ``type`` matches *channel*."""
        self._callbacks[channel].append(callback)

    # ── Main loop ───────────────────────────────────────────────────

    async def listen(self) -> None:
        """Receive messages and dispatch to callbacks.

        Blocks until :meth:`close` is called.  Automatically reconnects
        on disconnect with exponential backoff (1 s → 60 s cap).
        """
        self._running = True
        backoff = 1
        while self._running:
            try:
                async for raw in self._ws:
                    msg: dict[str, Any] = json.loads(raw)
                    msg_type = msg.get("type", "")
                    for cb in self._callbacks.get(msg_type, []):
                        try:
                            cb(msg)
                        except Exception:
                            self._log.exception("ws_callback_error", channel=msg_type)
                # Clean exit from the async-for means connection closed normally.
                if not self._running:
                    break
            except ConnectionClosed:
                if not self._running:
                    break
                self._log.warning("ws_disconnected", backoff_s=backoff)

            # Reconnect loop
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            try:
                await self.connect()
                backoff = 1
            except Exception:
                self._log.exception("ws_reconnect_failed")

    # ── Shutdown ────────────────────────────────────────────────────

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    # ── Internal ────────────────────────────────────────────────────

    async def _send(self, msg: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(msg))
