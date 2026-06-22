"""
Delta Exchange India REST client with HMAC-SHA256 authentication.

Testnet and live endpoints are selected automatically via ``Settings``.
All methods are synchronous, matching the sync DB layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from src.brokers.base import (
    BalanceInfo,
    Broker,
    CancelResult,
    OpenOrder,
    OrderRequest,
    OrderResult,
    OrderType,
    PositionInfo,
)
from src.core.config import Settings, get_settings
from src.core.logging import get_logger


class DeltaAPIError(Exception):
    """Raised when the Delta API returns ``"success": false``."""

    def __init__(
        self, code: str, message: str, context: dict[str, Any] | None = None
    ) -> None:
        self.code = code
        self.context = context or {}
        super().__init__(f"Delta API error [{code}]: {message}")


_STATUS_MAP: dict[str, str] = {
    "open": "open",
    "pending": "pending",
    "closed": "filled",
    "cancelled": "canceled",
}


class DeltaIndiaClient(Broker):
    """Synchronous REST client for Delta Exchange India.

    Usage::

        with DeltaIndiaClient() as client:
            client.set_leverage("BTCUSD", Decimal("5"))
            result = client.place_order(request)
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Construct a Delta India client.

        Pass explicit ``api_key`` / ``api_secret`` / ``base_url`` to target a
        specific sub-account (Decision 019). When omitted, credentials fall
        back to the active-mode ``Settings`` properties (the original single
        "default" account) so smoke scripts and tests keep working.
        """
        self._settings = settings or get_settings()
        self._log = get_logger("brokers.delta_india")
        self._api_key = api_key or self._settings.delta_api_key.get_secret_value()
        self._api_secret = (
            api_secret or self._settings.delta_api_secret.get_secret_value()
        )
        self._http = httpx.Client(
            base_url=base_url or self._settings.delta_base_url,
            timeout=10.0,
            headers={"User-Agent": "trading-bot/0.1.0"},
        )
        self._products: dict[str, dict[str, Any]] | None = None

    # ── HMAC signing ────────────────────────────────────────────────

    def _sign_headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
    ) -> dict[str, str]:
        timestamp = str(int(time.time()))
        # Prehash: METHOD + TIMESTAMP + PATH + QUERY_STRING + BODY
        # Query string is WITHOUT the leading '?'.
        message = method + timestamp + path + query_string + body
        sig = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self._api_key,
            "timestamp": timestamp,
            "signature": sig,
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ────────────────────────────────────────────────

    def _handle_response(self, resp: httpx.Response) -> Any:
        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            resp.raise_for_status()
            raise DeltaAPIError(
                code="parse_error",
                message=f"Non-JSON response (HTTP {resp.status_code})",
            ) from exc
        if not data.get("success"):
            error = data.get("error", {})
            raise DeltaAPIError(
                code=error.get("code", "unknown"),
                message=str(error),
                context=error.get("context"),
            )
        return data["result"]

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> Any:
        query_string = ""
        if params:
            filtered = {k: str(v) for k, v in params.items() if v is not None}
            if filtered:
                query_string = urlencode(sorted(filtered.items()))
        url = f"{path}?{query_string}" if query_string else path
        headers = self._sign_headers("GET", path, query_string) if auth else {}
        resp = self._http.get(url, headers=headers)
        return self._handle_response(resp)

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        body_str = json.dumps(body) if body else ""
        headers = self._sign_headers("POST", path, body=body_str)
        resp = self._http.post(path, content=body_str or None, headers=headers)
        return self._handle_response(resp)

    def _delete(self, path: str, body: dict[str, Any] | None = None) -> Any:
        body_str = json.dumps(body) if body else ""
        headers = self._sign_headers("DELETE", path, body=body_str)
        resp = self._http.request("DELETE", path, content=body_str or None, headers=headers)
        return self._handle_response(resp)

    # ── Product resolution ──────────────────────────────────────────

    def _ensure_products(self) -> None:
        """Lazy-load the product catalogue from ``GET /v2/products``."""
        if self._products is not None:
            return
        raw = self._get("/v2/products", auth=False)
        self._products = {}
        for p in raw:
            sym = p.get("symbol")
            if sym:
                self._products[sym] = p
        self._log.info("products_loaded", count=len(self._products))

    def _product_id(self, symbol: str) -> int:
        self._ensure_products()
        assert self._products is not None
        product = self._products.get(symbol)
        if product is None:
            raise ValueError(f"Unknown Delta symbol: {symbol}")
        return int(product["id"])

    def get_product_symbols(self) -> list[str]:
        """Return all listed product symbols (public, no auth)."""
        self._ensure_products()
        assert self._products is not None
        return list(self._products.keys())

    # ── Broker interface ────────────────────────────────────────────

    def place_order(self, request: OrderRequest) -> OrderResult:
        order_type_str = f"{request.order_type.value}_order"
        body: dict[str, Any] = {
            "product_symbol": request.symbol,
            "size": int(request.size),
            "side": request.side,
            "order_type": order_type_str,
        }
        if request.order_type == OrderType.LIMIT and request.limit_price is not None:
            body["limit_price"] = str(request.limit_price)
        if request.time_in_force:
            body["time_in_force"] = request.time_in_force.value
        if request.reduce_only:
            body["reduce_only"] = "true"
        if request.client_order_id:
            # Delta enforces max 32 characters for client_order_id.
            body["client_order_id"] = request.client_order_id[:32]

        self._log.info(
            "placing_order",
            symbol=request.symbol,
            side=request.side,
            size=str(request.size),
            order_type=order_type_str,
        )
        result = self._post("/v2/orders", body)

        return OrderResult(
            exchange_order_id=str(result["id"]),
            client_order_id=result.get("client_order_id"),
            symbol=result.get("product_symbol", request.symbol),
            side=result["side"],
            size=Decimal(str(result["size"])),
            price=(
                Decimal(str(result["limit_price"])) if result.get("limit_price") else None
            ),
            status=_STATUS_MAP.get(result.get("state", ""), "unknown"),
            raw=result,
        )

    def cancel_order(
        self,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
        symbol: str,
    ) -> CancelResult:
        if exchange_order_id is None and client_order_id is None:
            raise ValueError("Provide exchange_order_id or client_order_id")

        # Look up by client_order_id if exchange_order_id wasn't given.
        if exchange_order_id is None:
            order_info = self._get(f"/v2/orders/client_order_id/{client_order_id}")
            exchange_order_id = str(order_info["id"])

        product_id = self._product_id(symbol)
        body = {"id": int(exchange_order_id), "product_id": product_id}
        self._log.info("canceling_order", order_id=exchange_order_id, symbol=symbol)
        result = self._delete("/v2/orders", body)

        return CancelResult(
            exchange_order_id=exchange_order_id,
            symbol=symbol,
            success=True,
            raw=result,
        )

    def get_positions(self) -> list[PositionInfo]:
        result = self._get("/v2/positions/margined")
        positions: list[PositionInfo] = []
        for p in result:
            size = Decimal(str(p.get("size", 0)))
            if size == 0:
                continue
            side = "long" if size > 0 else "short"
            positions.append(
                PositionInfo(
                    symbol=p.get("product_symbol", str(p.get("product_id", ""))),
                    side=side,
                    size=abs(size),
                    entry_price=Decimal(str(p.get("entry_price", 0))),
                    liquidation_price=(
                        Decimal(str(p["liquidation_price"]))
                        if p.get("liquidation_price")
                        else None
                    ),
                    unrealized_pnl=(
                        Decimal(str(p["unrealized_pnl"]))
                        if p.get("unrealized_pnl")
                        else None
                    ),
                    raw=p,
                )
            )
        return positions

    def get_balances(self) -> list[BalanceInfo]:
        result = self._get("/v2/wallet/balances")
        balances: list[BalanceInfo] = []
        for b in result:
            balances.append(
                BalanceInfo(
                    asset=b.get("asset_symbol", str(b.get("asset_id", ""))),
                    available=Decimal(str(b.get("available_balance", 0))),
                    order_margin=Decimal(str(b.get("order_margin", 0))),
                    position_margin=Decimal(str(b.get("position_margin", 0))),
                    raw=b,
                )
            )
        return balances

    def get_open_orders(self, symbol: str | None = None) -> list[OpenOrder]:
        params: dict[str, Any] = {}
        if symbol:
            params["product_id"] = self._product_id(symbol)
        result = self._get("/v2/orders", params or None)
        orders: list[OpenOrder] = []
        for o in result:
            orders.append(
                OpenOrder(
                    exchange_order_id=str(o["id"]),
                    client_order_id=o.get("client_order_id"),
                    symbol=o.get("product_symbol", str(o.get("product_id", ""))),
                    side=o["side"],
                    size=Decimal(str(o["size"])),
                    unfilled_size=Decimal(str(o.get("unfilled_size", o["size"]))),
                    order_type=o.get("order_type", ""),
                    limit_price=(
                        Decimal(str(o["limit_price"])) if o.get("limit_price") else None
                    ),
                    status=_STATUS_MAP.get(o.get("state", ""), "unknown"),
                    raw=o,
                )
            )
        return orders

    def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        product_id = self._product_id(symbol)
        self._log.info("setting_leverage", symbol=symbol, leverage=str(leverage))
        self._post(
            f"/v2/products/{product_id}/orders/leverage",
            {"leverage": str(leverage)},
        )

    def get_order(self, exchange_order_id: str) -> OpenOrder | None:
        try:
            o = self._get(f"/v2/orders/{exchange_order_id}")
        except DeltaAPIError:
            return None
        return OpenOrder(
            exchange_order_id=str(o["id"]),
            client_order_id=o.get("client_order_id"),
            symbol=o.get("product_symbol", str(o.get("product_id", ""))),
            side=o["side"],
            size=Decimal(str(o["size"])),
            unfilled_size=Decimal(str(o.get("unfilled_size", o["size"]))),
            order_type=o.get("order_type", ""),
            limit_price=(
                Decimal(str(o["limit_price"])) if o.get("limit_price") else None
            ),
            status=_STATUS_MAP.get(o.get("state", ""), "unknown"),
            raw=o,
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DeltaIndiaClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
