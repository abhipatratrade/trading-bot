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
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from src.brokers.base import (
    BalanceInfo,
    Broker,
    CancelResult,
    FillInfo,
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


def _parse_ts(value: Any) -> datetime | None:
    """Parse Delta timestamps — ISO strings or epoch micro/seconds."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1e14:  # microseconds
                v /= 1e6
            return datetime.fromtimestamp(v, tz=UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


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
        self._products_fetched_at: float = 0.0  # monotonic
        # Seconds to ADD to local time when signing (clock-skew tolerance).
        # Re-learned from the server's Date header whenever Delta rejects
        # a signature as expired.
        self._time_offset: float = 0.0

    # ── HMAC signing ────────────────────────────────────────────────

    def _sign_headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
    ) -> dict[str, str]:
        timestamp = str(int(time.time() + self._time_offset))
        # Prehash: METHOD + TIMESTAMP + PATH + '?' + QUERY_STRING + BODY.
        # The '?' IS part of the signed message when a query string exists —
        # confirmed 2026-07-07 from the server's rejection signature_data
        # ('GET<ts>/v2/orders?states=open%2Cpending'). Without it every
        # authed GET that carries params 401s with Signature Mismatch.
        query_part = f"?{query_string}" if query_string else ""
        message = method + timestamp + path + query_part + body
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

    _MAX_ATTEMPTS = 3
    _BACKOFF_BASE_SECONDS = 0.5
    _RETRY_AFTER_CAP_SECONDS = 10.0

    def _resync_clock(self, resp: httpx.Response) -> None:
        """Learn the local↔server clock offset from the HTTP Date header."""
        date_header = resp.headers.get("Date")
        if not date_header:
            return
        try:
            server_now = parsedate_to_datetime(date_header).timestamp()
        except (ValueError, TypeError):
            return
        self._time_offset = server_now - time.time()
        self._log.warning(
            "delta_clock_resynced", offset_seconds=round(self._time_offset, 1)
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query_string: str = "",
        body_str: str = "",
        auth: bool = True,
        retry_transport: bool = False,
    ) -> Any:
        """Send one signed request with bounded, safety-aware retries.

        Retry policy per failure class:
          - HTTP 429: always retry (the request was rate-limited, never
            processed), honoring Retry-After capped at 10s.
          - expired/invalid-timestamp signature: resync the clock offset
            from the response's Date header and retry (rejected = safe).
          - transport errors / 5xx: retry ONLY when ``retry_transport``
            (GETs). For order placement the outcome is unknown — the
            OrderManager's client_order_id recovery owns that path.
        """
        url = f"{path}?{query_string}" if query_string else path
        last_exc: Exception | None = None
        for attempt in range(self._MAX_ATTEMPTS):
            backoff = self._BACKOFF_BASE_SECONDS * (2**attempt)
            # Signature includes the timestamp — rebuild every attempt.
            headers = (
                self._sign_headers(method, path, query_string, body_str)
                if auth
                else {}
            )
            try:
                resp = self._http.request(
                    method, url, content=body_str or None, headers=headers
                )
            except httpx.TransportError as exc:
                last_exc = exc
                if not retry_transport or attempt == self._MAX_ATTEMPTS - 1:
                    raise
                self._log.warning(
                    "delta_transport_retry", path=path, attempt=attempt + 1
                )
                time.sleep(backoff)
                continue

            if resp.status_code == 429 and attempt < self._MAX_ATTEMPTS - 1:
                try:
                    delay = float(resp.headers.get("Retry-After", backoff))
                except ValueError:
                    delay = backoff
                self._log.warning(
                    "delta_rate_limited", path=path, attempt=attempt + 1
                )
                time.sleep(min(max(delay, backoff), self._RETRY_AFTER_CAP_SECONDS))
                continue
            if (
                resp.status_code >= 500
                and retry_transport
                and attempt < self._MAX_ATTEMPTS - 1
            ):
                self._log.warning(
                    "delta_5xx_retry",
                    path=path,
                    status=resp.status_code,
                    attempt=attempt + 1,
                )
                time.sleep(backoff)
                continue

            try:
                return self._handle_response(resp)
            except DeltaAPIError as exc:
                code = str(exc.code)
                stale_sig = "expired" in code or "timestamp" in code
                if stale_sig and auth and attempt < self._MAX_ATTEMPTS - 1:
                    self._resync_clock(resp)
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("unreachable")  # pragma: no cover

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
        return self._request(
            "GET", path, query_string=query_string, auth=auth, retry_transport=True
        )

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        # No transport retry: a POST that died in transit may have landed
        # (order placement). 429 / stale-signature retries still apply.
        return self._request(
            "POST", path, body_str=json.dumps(body) if body else ""
        )

    def _delete(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request(
            "DELETE", path, body_str=json.dumps(body) if body else ""
        )

    # ── Product resolution ──────────────────────────────────────────

    _PRODUCTS_TTL_SECONDS = 6 * 3600

    def _ensure_products(self) -> None:
        """Load/refresh the product catalogue from ``GET /v2/products``.

        Refreshed every 6h so new listings, tick sizes, and contract
        values track the venue. A failed refresh keeps serving the stale
        catalogue (better than none) unless there has never been one.
        """
        fresh = (
            self._products is not None
            and time.monotonic() - self._products_fetched_at
            < self._PRODUCTS_TTL_SECONDS
        )
        if fresh:
            return
        try:
            raw = self._get("/v2/products", auth=False)
        except Exception:
            if self._products is not None:
                self._log.warning("products_refresh_failed_serving_stale")
                # Retry the refresh in ~10 min, not a full TTL from now.
                self._products_fetched_at = (
                    time.monotonic() - self._PRODUCTS_TTL_SECONDS + 600
                )
                return
            raise
        self._products = {}
        for p in raw:
            sym = p.get("symbol")
            if sym:
                self._products[sym] = p
        self._products_fetched_at = time.monotonic()
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

    def contract_size(
        self, symbol: str, default: Decimal | None = Decimal("1")
    ) -> Decimal | None:
        """Contract size from the live product spec (``contract_value``).

        Returns ``default`` when the product/field is missing or the
        catalogue fetch fails rather than raising. The sizer passes
        ``default=None`` so it can distinguish "venue says 1" from
        "venue doesn't know" and fall back to YAML.
        """
        try:
            self._ensure_products()
        except Exception:
            self._log.warning("contract_size_products_fetch_failed", symbol=symbol)
            return default
        assert self._products is not None
        product = self._products.get(symbol)
        if product is None:
            return default
        raw = product.get("contract_value")
        if raw is None:
            return default
        try:
            return Decimal(str(raw))
        except ArithmeticError:
            return default

    def tick_size(self, symbol: str) -> Decimal | None:
        """Price increment from the live product spec (``tick_size``)."""
        try:
            self._ensure_products()
        except Exception:
            self._log.warning("tick_size_products_fetch_failed", symbol=symbol)
            return None
        assert self._products is not None
        product = self._products.get(symbol)
        raw = product.get("tick_size") if product else None
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except ArithmeticError:
            return None

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
        if request.stop_price is not None:
            # Stop order (stop-market when order_type is market_order).
            # Trigger on mark price — last-traded can wick on thin books.
            body["stop_order_type"] = "stop_loss_order"
            body["stop_price"] = str(request.stop_price)
            body["stop_trigger_method"] = "mark_price"
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
        # "pending" includes untriggered stop orders — without it the
        # protective stop layer (Decision 022) would never see its stops.
        params: dict[str, Any] = {"states": "open,pending"}
        if symbol:
            params["product_id"] = self._product_id(symbol)
        result = self._get("/v2/orders", params)
        return [self._to_open_order(o) for o in result]

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
        return self._to_open_order(o)

    def get_order_by_client_id(self, client_order_id: str) -> OpenOrder | None:
        try:
            o = self._get(f"/v2/orders/client_order_id/{client_order_id}")
        except DeltaAPIError:
            return None
        return self._to_open_order(o)

    def get_fills(self, *, start_time: datetime | None = None) -> list[FillInfo]:
        """Fills for this (sub-)account, newest page only (page_size=500).

        ``start_time`` narrows the window (Delta expects **microseconds**
        since epoch). One page of 500 comfortably covers a multi-day
        window at Phase-1 trade frequency; pagination can come later.
        """
        params: dict[str, Any] = {"page_size": 500}
        if start_time is not None:
            params["start_time"] = int(start_time.timestamp() * 1_000_000)
        result = self._get("/v2/fills", params)
        fills: list[FillInfo] = []
        for f in result or []:
            try:
                fills.append(
                    FillInfo(
                        fill_id=str(f["id"]),
                        exchange_order_id=str(f.get("order_id", "")),
                        symbol=f.get("product_symbol", str(f.get("product_id", ""))),
                        side=str(f.get("side", "")),
                        size=Decimal(str(f.get("size", 0))),
                        price=Decimal(str(f.get("price", 0))),
                        commission=Decimal(str(f.get("commission", 0) or 0)),
                        timestamp=_parse_ts(f.get("created_at")),
                        raw=f,
                    )
                )
            except (KeyError, ArithmeticError, TypeError):
                self._log.warning("fill_parse_failed", fill=f)
        return fills

    def wallet_flow_totals(self) -> tuple[Decimal, Decimal] | None:
        """Deposit/withdrawal totals from ``GET /v2/wallet/transactions``.

        Classification by ``transaction_type``: *deposit* types add to
        deposits, *withdrawal* types to withdrawals, *transfer* types
        (sub-account funding) by amount sign. Trading cashflows (pnl,
        commission, funding) are ignored. One page of 500 covers Phase-1
        history; pagination can come later.
        """
        result = self._get("/v2/wallet/transactions", {"page_size": 500})
        rows = result.get("result", result) if isinstance(result, dict) else result
        deposits = Decimal("0")
        withdrawals = Decimal("0")
        for t in rows or []:
            ttype = str(t.get("transaction_type", "")).lower()
            try:
                amount = Decimal(str(t.get("amount", "0")))
            except ArithmeticError:
                continue
            if "deposit" in ttype:
                deposits += abs(amount)
            elif "withdrawal" in ttype:
                withdrawals += abs(amount)
            elif "transfer" in ttype:
                if amount >= 0:
                    deposits += amount
                else:
                    withdrawals += -amount
        return deposits, withdrawals

    @staticmethod
    def _to_open_order(o: dict[str, Any]) -> OpenOrder:
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
            stop_price=(
                Decimal(str(o["stop_price"])) if o.get("stop_price") else None
            ),
            reduce_only=str(o.get("reduce_only", "")).lower() == "true",
            raw=o,
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DeltaIndiaClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
