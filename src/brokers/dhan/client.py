"""
Dhan (DhanHQ) broker — REST order client for cash equities + MTF (Decision 012).

Implements the ``Broker`` interface against the Dhan v2 Orders/Positions/Funds
APIs. Orders go to the sandbox host (``sandbox.dhan.co``, testnet) or the live
host (``api.dhan.co``), selected by ``trading_mode`` (House Rule #6). The
access token is provided by ``DhanTokenManager`` — a static DevPortal token in
sandbox, or the TOTP-refreshed live token in live mode.

Symbols are human tickers (e.g. ``"SWIGGY"``); Dhan needs a numeric
``securityId`` + ``exchangeSegment``, resolved via the injected ``resolve_symbol``
callable (``DhanData.resolve``). Responses carry ``tradingSymbol`` back, so the
reverse map is free.

Order-type / leverage notes:
  * Cash equity has no leverage knob — MTF is a fixed broker-funded multiple, so
    ``set_leverage`` is a no-op. ``productType=MTF`` requests margin funding;
    non-MTF-approved scrips reject, and (when enabled) we retry the same order
    as ``CNC`` (1× delivery).
  * ``reduce_only`` has no Dhan equivalent for equities (a close is just an
    opposite-side order); we ignore it on entries and rely on the exit/stop
    layers placing correctly-sided orders.
  * A protective stop (Decision 022) is a SELL ``STOP_LOSS_MARKET`` with a
    ``triggerPrice`` — set via ``OrderRequest.stop_price``.

Idempotency: Dhan does NOT dedupe by ``correlationId`` server-side, so it is a
tag, not a guard. Double-fire safety comes from the OrderManager's
client_order_id recovery — ``get_order_by_client_id`` scans the day's orders for
the correlationId so a transport-error retry finds an order that already landed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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
from src.brokers.dhan.auth import DhanTokenManager
from src.core.config import Settings, get_settings
from src.core.logging import get_logger

# NSE/BSE cash tick size (₹0.05) — used to snap protective-stop triggers.
_DEFAULT_TICK = Decimal("0.05")

# Dhan orderStatus → canonical status (matches the Delta client's vocabulary).
_STATUS_MAP: dict[str, str] = {
    "TRANSIT": "pending",
    "PENDING": "pending",
    "OPEN": "open",
    "TRADED": "filled",
    "EXECUTED": "filled",
    "PART_TRADED": "partial",
    "PARTIALLY_FILLED": "partial",
    "REJECTED": "rejected",
    "CANCELLED": "canceled",
    "EXPIRED": "expired",
}

# Order states Dhan considers still working (for get_open_orders).
_OPEN_STATES = {"TRANSIT", "PENDING", "OPEN", "PART_TRADED", "PARTIALLY_FILLED"}

ResolveSymbol = Callable[[str], tuple[str, str]]  # ticker -> (security_id, exchange)


class DhanAPIError(Exception):
    """Raised when the Dhan API returns an error envelope or non-2xx status."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"Dhan API error [{code}]: {message}")


def is_invalid_token_error(exc: BaseException) -> bool:
    """True when ``exc`` is a Dhan single-session token invalidation (DH-906).

    Used by the caller to distinguish a self-healing token eviction (a second
    Dhan session was created — e.g. the user logged into the app — which the
    bot recovers from once Dhan's "one token / 2 min" cooldown clears) from a
    genuine safety-path failure that must page immediately.
    """
    if isinstance(exc, DhanAPIError):
        return exc.code.upper() == "DH-906" or "invalid token" in str(exc).lower()
    return False


def is_transient_upstream_error(exc: BaseException) -> bool:
    """True when ``exc`` is Dhan being briefly unwell, not the bot being wrong.

    A 5xx or a dropped/timed-out connection is upstream weather: the safety
    sweeps re-run every 60s and the next one almost always succeeds. The Dhan
    502 on ``/v2/positions`` at 08:20 IST on 2026-08-09 recovered on the very
    next tick and still paged three times.

    Deliberately narrow. 4xx is NOT transient — that is the bot sending
    something wrong, and it will keep being wrong until someone looks. Nor is
    DH-906, which has its own longer grace because it self-heals on a known
    schedule; ``is_invalid_token_error`` is checked first by the caller.
    """
    if isinstance(exc, DhanAPIError):
        return exc.code.isdigit() and 500 <= int(exc.code) <= 599
    return isinstance(exc, httpx.TransportError)


def _is_invalid_token(resp: httpx.Response) -> bool:
    """True when a response means the access token was rejected.

    Two shapes: a plain HTTP 401, or a 400/200 with Dhan's error envelope
    ``{"errorCode": "DH-906", "errorMessage": "Invalid Token"}`` — the form the
    live trading endpoints return on a single-session invalidation.
    """
    if resp.status_code == 401:
        return True
    try:
        data = resp.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    code = str(data.get("errorCode", "")).upper()
    msg = str(data.get("errorMessage", "")).lower()
    return code == "DH-906" or "invalid token" in msg


class DhanClient(Broker):
    """Synchronous REST order client for Dhan (cash equity + MTF)."""

    def __init__(
        self,
        *,
        token_manager: DhanTokenManager,
        client_id: str,
        resolve_symbol: ResolveSymbol,
        base_url: str = "https://sandbox.dhan.co",
        product_type: str = "MTF",
        mtf_fallback_cnc: bool = True,
        http: httpx.Client | None = None,
    ) -> None:
        self._token = token_manager
        self._client_id = client_id
        self._resolve = resolve_symbol
        self._base = base_url.rstrip("/")
        self._product_type = product_type
        self._mtf_fallback_cnc = mtf_fallback_cnc
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self._log = get_logger("brokers.dhan")

    @classmethod
    def from_settings(
        cls,
        resolve_symbol: ResolveSymbol,
        settings: Settings | None = None,
        *,
        data_token_manager: DhanTokenManager | None = None,
        product_type: str = "MTF",
    ) -> DhanClient:
        """Build a client for the active mode (House Rule #6).

        In testnet the order token is the static DevPortal sandbox token; in
        live mode ``order_token`` is None and we reuse the refreshable data
        token manager (``data_token_manager``), which the caller already built
        for market data.
        """
        s = settings or get_settings()
        acct = s.dhan_account()
        if acct.order_token is not None:
            token = DhanTokenManager(static_token=acct.order_token)
        elif data_token_manager is not None:
            token = data_token_manager  # live: orders reuse the live data token
        else:
            token = DhanTokenManager(
                client_id=acct.order_client_id,
                pin=acct.pin,
                totp_secret=acct.totp_secret,
            )
        assert acct.order_client_id is not None
        return cls(
            token_manager=token,
            client_id=acct.order_client_id,
            resolve_symbol=resolve_symbol,
            base_url=acct.order_base_url,
            product_type=product_type,
        )

    # ── HTTP ────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Send one request, refreshing the token once on an invalid-token error.

        A token can be rejected two ways: HTTP 401, OR — on the live trading
        endpoints — an HTTP 400 with envelope ``errorCode DH-906 / "Invalid
        Token"``. The latter is what a single-session invalidation looks like
        (a peer process minted, killing ours server-side while it is still
        valid by its own timestamp). Both trigger invalidate() + one retry;
        the token manager then adopts the peer's fresh cached token.
        """
        def _send() -> httpx.Response:
            return self._http.request(
                method,
                f"{self._base}{path}",
                json=body,
                headers={
                    "access-token": self._token.token(),
                    "client-id": self._client_id,
                    "Content-Type": "application/json",
                },
            )

        resp = _send()
        if _is_invalid_token(resp):
            self._token.invalidate()
            resp = _send()
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            # Non-JSON body — e.g. a 403 IP-block page or an empty error
            # response. Surface it as a clean DhanAPIError (with the status
            # code) instead of leaking httpx/json internals into the logs.
            body = resp.text[:200] or f"empty body (HTTP {resp.status_code})"
            raise DhanAPIError(str(resp.status_code), body) from None
        # Dhan error envelope: {"errorType":..,"errorCode":..,"errorMessage":..}
        if isinstance(data, dict) and (data.get("errorType") or data.get("errorCode")):
            raise DhanAPIError(
                str(data.get("errorCode", "unknown")),
                str(data.get("errorMessage", data)),
            )
        if resp.status_code >= 400:
            raise DhanAPIError(str(resp.status_code), resp.text[:200])
        return data

    # ── Broker interface ────────────────────────────────────────────────
    def place_order(self, request: OrderRequest) -> OrderResult:
        security_id, exchange = self._resolve(request.symbol)
        # Product is per-ORDER (Decision 029): swing-indian sends MTF and
        # intraday-indian sends INTRADAY through this same client, because
        # Dhan has one account and therefore one adapter instance.
        product_type = request.product or self._product_type
        body = self._order_body(request, security_id, exchange, product_type)

        self._log.info(
            "placing_order",
            symbol=request.symbol,
            side=request.side,
            size=str(request.size),
            product=product_type,
            stop=str(request.stop_price) if request.stop_price is not None else None,
        )
        effective = request
        try:
            result = self._request("POST", "/v2/orders", body)
        except DhanAPIError:
            # A scrip the venue grants no MTF (or no MIS) on rejects the order.
            # Decision 029, amended 2026-07-27 (user decision): trade it 1× CNC
            # rather than skip it — but the size MUST be clamped to
            # ``fallback_max_size``. ``size`` was computed at the leveraged
            # product's multiple, so re-sending it on a cash product would
            # consume `leverage`× the margin the sizer budgeted for the slot
            # (at swing-indian's ~3.8× MTF, a ₹10k slot would spend ~₹38k of
            # cash — on an account shared with the user's own money). No cap
            # supplied ⇒ no fallback, and the rejection stands.
            leveraged = product_type == "MTF" and self._mtf_fallback_cnc
            if (
                leveraged or product_type == "INTRADAY"
            ) and request.fallback_max_size is not None:
                capped = min(request.size, request.fallback_max_size)
                if capped < 1:
                    raise
                effective = replace(request, size=capped)
                self._log.warning(
                    "leveraged_product_rejected_retry_cnc_1x",
                    symbol=request.symbol,
                    product=product_type,
                    requested_size=str(request.size),
                    cnc_size=str(capped),
                )
                body = self._order_body(effective, security_id, exchange, "CNC")
                result = self._request("POST", "/v2/orders", body)
            else:
                raise

        product = body["productType"]
        return OrderResult(
            exchange_order_id=str(result.get("orderId", "")),
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            # The size actually accepted — a CNC fallback may have clamped it.
            size=effective.size,
            price=None,  # market order — fill price comes from get_fills
            status=_STATUS_MAP.get(str(result.get("orderStatus", "")).upper(), "pending"),
            raw={**result, "productType": product},
        )

    def _order_body(
        self,
        request: OrderRequest,
        security_id: str,
        exchange: str,
        product_type: str,
    ) -> dict[str, Any]:
        is_stop = request.stop_price is not None
        if is_stop:
            order_type = "STOP_LOSS_MARKET"
        elif request.order_type == OrderType.LIMIT:
            order_type = "LIMIT"
        else:
            order_type = "MARKET"
        body: dict[str, Any] = {
            "dhanClientId": self._client_id,
            "transactionType": request.side.upper(),  # BUY | SELL
            "exchangeSegment": exchange,               # NSE_EQ | BSE_EQ
            "productType": product_type,               # MTF | CNC | INTRADAY
            "orderType": order_type,
            "validity": "DAY",
            "securityId": security_id,
            "quantity": int(request.size),
            "disclosedQuantity": 0,
            "afterMarketOrder": False,
        }
        if request.order_type == OrderType.LIMIT and request.limit_price is not None:
            body["price"] = float(request.limit_price)
        else:
            body["price"] = 0
        if is_stop:
            body["triggerPrice"] = float(self._snap_tick(request.stop_price))  # type: ignore[arg-type]
        if request.client_order_id:
            body["correlationId"] = request.client_order_id[:25]  # Dhan cap
        return body

    @staticmethod
    def _snap_tick(price: Decimal) -> Decimal:
        """Snap a price onto the ₹0.05 grid (Dhan rejects off-tick triggers)."""
        return (price / _DEFAULT_TICK).quantize(Decimal("1")) * _DEFAULT_TICK

    def cancel_order(
        self,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
        symbol: str,  # noqa: ARG002 — Dhan cancels by order id alone
    ) -> CancelResult:
        if exchange_order_id is None and client_order_id is None:
            raise ValueError("Provide exchange_order_id or client_order_id")
        if exchange_order_id is None:
            order = self.get_order_by_client_id(client_order_id or "")
            if order is None:
                return CancelResult(
                    exchange_order_id="", symbol=symbol, success=False,
                    raw={"error": "correlationId not found"},
                )
            exchange_order_id = order.exchange_order_id
        result = self._request("DELETE", f"/v2/orders/{exchange_order_id}")
        status = str(result.get("orderStatus", "")).upper() if isinstance(result, dict) else ""
        return CancelResult(
            exchange_order_id=exchange_order_id,
            symbol=symbol,
            success=status in {"CANCELLED", ""},
            raw=result if isinstance(result, dict) else {"result": result},
        )

    def get_positions(self) -> list[PositionInfo]:
        result = self._request("GET", "/v2/positions") or []
        positions: list[PositionInfo] = []
        for p in result:
            net = Decimal(str(p.get("netQty", 0) or 0))
            if net == 0:  # CLOSED / squared-off
                continue
            side = "long" if net > 0 else "short"
            avg = p.get("buyAvg") if net > 0 else p.get("sellAvg")
            positions.append(
                PositionInfo(
                    symbol=p.get("tradingSymbol", str(p.get("securityId", ""))),
                    side=side,
                    size=abs(net),
                    entry_price=Decimal(str(avg or p.get("costPrice", 0) or 0)),
                    unrealized_pnl=(
                        Decimal(str(p["unrealizedProfit"]))
                        if p.get("unrealizedProfit") is not None
                        else None
                    ),
                    raw=p,
                )
            )
        return positions

    def get_balances(self) -> list[BalanceInfo]:
        result = self._request("GET", "/v2/fundlimit") or {}
        avail = Decimal(str(result.get("availabelBalance", 0) or 0))  # Dhan's spelling
        utilized = Decimal(str(result.get("utilizedAmount", 0) or 0))
        return [
            BalanceInfo(
                asset="INR",
                available=avail,
                order_margin=Decimal("0"),
                position_margin=utilized,
                raw=result if isinstance(result, dict) else {"result": result},
            )
        ]

    def get_open_orders(self, symbol: str | None = None) -> list[OpenOrder]:
        result = self._request("GET", "/v2/orders") or []
        orders = [self._to_open_order(o) for o in result
                  if str(o.get("orderStatus", "")).upper() in _OPEN_STATES]
        if symbol is not None:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    def required_margin(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        product: str | None = None,
    ) -> Decimal | None:
        """Ask Dhan what margin this order needs (``/v2/margincalculator``).

        Intraday leverage is per-scrip, so this is the only honest way to know
        the size we can actually afford — we never predict a multiple, we ask
        for the rupee figure and fit to it.

        Returns None on ANY failure (endpoint unavailable, unparseable, scrip
        unresolvable). None means "unknown", and the caller must degrade to a
        guaranteed-affordable size rather than assume the bucket's leverage
        was granted.

        NOTE: unexercised against a live Dhan account as of 2026-07-21 —
        nothing in this repo or ``scripts/dhan-scanner/`` has called it. Treat
        the first sandbox soak as its acceptance test; until then the caller's
        1x fallback is what actually governs size.
        """
        try:
            security_id, exchange = self._resolve(symbol)
            result = self._request(
                "POST",
                "/v2/margincalculator",
                {
                    "dhanClientId": self._client_id,
                    "exchangeSegment": exchange,
                    "transactionType": side.upper(),
                    "quantity": int(quantity),
                    "productType": product or self._product_type,
                    "securityId": security_id,
                    "price": float(price),
                },
            )
            if not isinstance(result, dict):
                return None
            raw = result.get("totalMargin", result.get("total_margin"))
            if raw is None:
                return None
            margin = Decimal(str(raw))
            return margin if margin > 0 else None
        except Exception:
            self._log.warning(
                "margin_preflight_failed", symbol=symbol, exc_info=True
            )
            return None

    def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        # MTF is a fixed broker-funded multiple; there is no per-order leverage
        # knob for cash equity. No-op (keeps the Broker contract satisfied).
        self._log.debug("set_leverage_noop_mtf", symbol=symbol, leverage=str(leverage))

    def get_order(self, exchange_order_id: str) -> OpenOrder | None:
        try:
            result = self._request("GET", f"/v2/orders/{exchange_order_id}")
        except DhanAPIError:
            return None
        # Dhan returns a single-element list or an object depending on endpoint.
        row = result[0] if isinstance(result, list) and result else result
        return self._to_open_order(row) if isinstance(row, dict) and row else None

    def get_order_by_client_id(self, client_order_id: str) -> OpenOrder | None:
        """Find an order by correlationId (idempotency-recovery path).

        Dhan has no dedupe-by-correlationId, so we scan the day's orders. This
        lets the OrderManager confirm whether a transport-failed POST actually
        landed before retrying.
        """
        cid = client_order_id[:25]
        try:
            result = self._request("GET", "/v2/orders") or []
        except DhanAPIError:
            return None
        for o in result:
            if o.get("correlationId") == cid:
                return self._to_open_order(o)
        return None

    def get_fills(self, *, start_time: datetime | None = None) -> list[FillInfo]:  # noqa: ARG002
        """Executed trades for today (``GET /v2/trades``).

        Dhan's trade book is intraday-scoped, so ``start_time`` is ignored —
        the day's fills are what the reconciler enriches P&L from.
        """
        try:
            result = self._request("GET", "/v2/trades") or []
        except DhanAPIError:
            return []
        fills: list[FillInfo] = []
        for f in result:
            try:
                qty = Decimal(str(f.get("tradedQuantity", f.get("quantity", 0)) or 0))
                fills.append(
                    FillInfo(
                        fill_id=str(f.get("exchangeTradeId", f.get("orderId", ""))),
                        exchange_order_id=str(f.get("orderId", "")),
                        symbol=f.get("tradingSymbol", str(f.get("securityId", ""))),
                        side=str(f.get("transactionType", "")).lower(),
                        size=qty,
                        price=Decimal(str(f.get("tradedPrice", f.get("price", 0)) or 0)),
                        commission=Decimal("0"),  # not in trade book; fees via ledger
                        timestamp=_parse_ts(f.get("createTime") or f.get("exchangeTime")),
                        raw=f,
                    )
                )
            except (KeyError, ArithmeticError, TypeError):
                self._log.warning("fill_parse_failed", fill=f)
        return fills

    def contract_size(
        self, symbol: str, default: Decimal | None = Decimal("1")  # noqa: ARG002
    ) -> Decimal | None:
        # One share per unit for cash equity.
        return Decimal("1")

    def tick_size(self, symbol: str) -> Decimal | None:  # noqa: ARG002
        return _DEFAULT_TICK

    def _to_open_order(self, o: dict[str, Any]) -> OpenOrder:
        qty = Decimal(str(o.get("quantity", 0) or 0))
        filled = Decimal(str(o.get("filledQty", o.get("tradedQty", 0)) or 0))
        trig = o.get("triggerPrice")
        return OpenOrder(
            exchange_order_id=str(o.get("orderId", "")),
            client_order_id=o.get("correlationId"),
            symbol=o.get("tradingSymbol", str(o.get("securityId", ""))),
            side=str(o.get("transactionType", "")).lower(),
            size=qty,
            unfilled_size=qty - filled,
            order_type=str(o.get("orderType", "")),
            limit_price=(
                Decimal(str(o["price"])) if o.get("price") else None
            ),
            status=_STATUS_MAP.get(str(o.get("orderStatus", "")).upper(), "unknown"),
            stop_price=Decimal(str(trig)) if trig else None,
            reduce_only=False,
            created_at=_parse_ts(o.get("createTime")),
            raw=o,
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> DhanClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _parse_ts(value: Any) -> datetime | None:
    """Parse Dhan timestamps (ISO strings or epoch seconds)."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None
