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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx

from src.brokers.base import (
    AttachedStopRetireError,
    BalanceInfo,
    Broker,
    CancelResult,
    FillInfo,
    OpenOrder,
    OrderCharges,
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

# Super Order endpoints (Decision 034). Entry + target + stop-loss in ONE
# request, so a stop the venue refuses means the ENTRY never happens.
_SUPER_PATH = "/v2/super/orders"
_PLAIN_PATH = "/v2/orders"

# Dhan's leg names. One orderId covers all three; the leg is addressed by name.
_ENTRY_LEG = "ENTRY_LEG"
_TARGET_LEG = "TARGET_LEG"
_STOP_LOSS_LEG = "STOP_LOSS_LEG"

# Super-order leg states that still rest at the venue.
_LEG_LIVE_STATES = {"PENDING", "TRANSIT", "OPEN", "PART_TRADED", "PARTIALLY_FILLED"}

# Hard ceiling on trade-history pagination. The page size is undocumented, so
# the walk stops on an empty page; this only bounds a pathological loop.
_MAX_CHARGE_PAGES = 50

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




# Dhan returns the literal STRING "NA" for an order placed without a
# correlation id — not null, not "". bool("NA") is True, so a naive truth test
# reads every one of the user's manually-placed orders as the bot's own.
#
# That is not cosmetic. plan_stop_protection CANCELS the resting stops it
# believes are its own, so on 2026-08-14 the sweep put the user's hand-placed
# PIIND stop on the cancel list — exactly the Decision 027 violation the
# correlationId check was added to prevent. The bot must be able to prove an
# order is its own, and "NA" proves the opposite.
_NOT_OURS = {"", "na", "none", "null", "0"}


def _is_ours(correlation_id: object) -> bool:
    """True only when Dhan echoes back a correlation id WE set."""
    return str(correlation_id or "").strip().lower() not in _NOT_OURS


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
        owns_order_id: Callable[[str], bool] | None = None,
    ) -> None:
        self._token = token_manager
        self._client_id = client_id
        self._resolve = resolve_symbol
        self._base = base_url.rstrip("/")
        self._product_type = product_type
        self._mtf_fallback_cnc = mtf_fallback_cnc
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        # Ledger-backed ownership proof for super orders, injected because this
        # adapter stays DB-free (the backtester imports it). See
        # ``_owns_super_order`` for why correlationId alone is not enough.
        self._owns_order_id = owns_order_id
        # Ids the ledger has already confirmed as ours. Bounded by the number of
        # super orders this process places; the alternative is a DB round-trip
        # per order per lookup, and the sweep does three lookups a tick against
        # a rate-limited account.
        self._owned_ids: set[str] = set()
        self._log = get_logger("brokers.dhan")

    @classmethod
    def from_settings(
        cls,
        resolve_symbol: ResolveSymbol,
        settings: Settings | None = None,
        *,
        data_token_manager: DhanTokenManager | None = None,
        product_type: str = "MTF",
        owns_order_id: Callable[[str], bool] | None = None,
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
            owns_order_id=owns_order_id,
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
        is_super = request.attached_stop_price is not None

        # THE NAKED-SHORT GUARD (Decision 034). A super order's STOP_LOSS_LEG
        # rests at the venue independently of our position. If a strategy exit
        # (or a breaker flatten) sells the position while that leg still rests,
        # the leg later triggers against stock we no longer hold — on MTF that
        # OPENS A SHORT, which is a far worse failure than the missing stop this
        # decision exists to fix.
        #
        # Every closing path in this repo — BucketRunner._close_position and
        # safety.enforcement._flatten_positions — funnels into place_order with
        # reduce_only=True, so retiring the leg HERE covers all of them, plus any
        # path written later. Doing it in the adapter rather than in OrderManager
        # also keeps the broker-agnostic layer free of Dhan leg semantics.
        #
        # A protective stop is itself reduce-only, so the discriminator is
        # ``stop_price is None`` — a closing MARKET order, not a resting stop.
        # Raises on failure: an un-retired leg must abort the close, because a
        # stale stop is survivable and a double-sell is not.
        if request.reduce_only and request.stop_price is None and not is_super:
            self._retire_attached_stop(request.symbol)

        path = _SUPER_PATH if is_super else _PLAIN_PATH
        body = (
            self._super_order_body(request, security_id, exchange, product_type)
            if is_super
            else self._order_body(request, security_id, exchange, product_type)
        )

        self._log.info(
            "placing_order",
            symbol=request.symbol,
            side=request.side,
            size=str(request.size),
            product=product_type,
            super_order=is_super,
            stop=str(request.stop_price) if request.stop_price is not None else None,
            attached_stop=(
                str(request.attached_stop_price) if is_super else None
            ),
        )
        effective = request
        try:
            result = self._request("POST", path, body)
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
                # The fallback must stay on the SAME endpoint: a super order
                # retried as a plain order would drop the attached stop and
                # place exactly the unprotected entry Decision 034 removes.
                body = (
                    self._super_order_body(effective, security_id, exchange, "CNC")
                    if is_super
                    else self._order_body(effective, security_id, exchange, "CNC")
                )
                result = self._request("POST", path, body)
            else:
                raise

        product = body["productType"]
        order_id = str(result.get("orderId", ""))

        # Retire the target leg Dhan forced us to declare. Done AFTER the entry
        # is accepted, because the entry is what we actually wanted; a target
        # that survives is a real exit at a price no backtest justifies (House
        # Rule 7), so failure here pages rather than passing silently.
        target_cancelled = None
        if is_super and order_id:
            target_cancelled = self._retire_target_leg(order_id, request.symbol)

        return OrderResult(
            exchange_order_id=order_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            # The size actually accepted — a CNC fallback may have clamped it.
            size=effective.size,
            price=None,  # market order — fill price comes from get_fills
            status=_STATUS_MAP.get(str(result.get("orderStatus", "")).upper(), "pending"),
            raw={
                **result,
                "productType": product,
                **(
                    {
                        "_super_order": True,
                        "_attached_stop": str(request.attached_stop_price),
                        "_target_leg_cancelled": target_cancelled,
                    }
                    if is_super
                    else {}
                ),
            },
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

    # ── Super orders (Decision 034) ─────────────────────────────────────
    def supports_attached_stop(self) -> bool:
        """Dhan carries the stop on the entry itself, via Super Order."""
        return True

    def _super_order_body(
        self,
        request: OrderRequest,
        security_id: str,
        exchange: str,
        product_type: str,
    ) -> dict[str, Any]:
        """Entry + target + stop in one request body.

        Deliberately limited to the fields Dhan documents for this endpoint.
        The plain-order body carries ``validity`` / ``disclosedQuantity`` /
        ``afterMarketOrder``; those are not in the super-order spec and this
        integration has been bitten too often by sending fields the venue did
        not ask for.

        ``targetPrice`` is MANDATORY here and no strategy in this repo has a
        target, so the caller supplies one it can live with and we cancel the
        leg immediately after placement. We refuse to invent one: the target
        has to sit inside the scrip's daily circuit band or Dhan rejects the
        WHOLE super order (the entry included), and the band is known to the
        caller, not to this adapter. Guessing here would turn a stop-placement
        failure into an entry-placement failure — the same 2026-08-12 outage
        with a different victim.
        """
        if request.attached_target_price is None:
            raise ValueError(
                f"super order for {request.symbol} needs attached_target_price "
                "(Dhan requires a TARGET_LEG); caller must supply one it can "
                "cancel"
            )
        body: dict[str, Any] = {
            "dhanClientId": self._client_id,
            "transactionType": request.side.upper(),   # BUY | SELL
            "exchangeSegment": exchange,               # NSE_EQ | BSE_EQ
            "productType": product_type,               # MTF | CNC | INTRADAY
            "orderType": (
                "LIMIT" if request.order_type == OrderType.LIMIT else "MARKET"
            ),
            "securityId": security_id,
            "quantity": int(request.size),
            "price": (
                float(request.limit_price)
                if request.order_type == OrderType.LIMIT
                and request.limit_price is not None
                else 0
            ),
            "targetPrice": float(self._snap_tick(request.attached_target_price)),
            "stopLossPrice": float(self._snap_tick(request.attached_stop_price)),  # type: ignore[arg-type]
            # Explicitly no trailing: Dhan's spec says a jump of 0 cancels the
            # trailing behaviour. Our stops are fixed at entry by design
            # (Decision 032 — the distance the backtest validated).
            "trailingJump": 0,
        }
        if request.client_order_id:
            body["correlationId"] = request.client_order_id[:25]
        return body

    def _retire_target_leg(self, order_id: str, symbol: str) -> bool:
        """Cancel the target leg. Never raises — the ENTRY already landed.

        Raising would send ``OrderManager`` down its transport-error recovery
        path for an order that was accepted, so failure is reported as False
        and left for :meth:`retire_stale_target_legs` to retry. The position is
        protected either way; what is at risk is an unbacktested profit-take.
        """
        try:
            self._request("DELETE", f"{_SUPER_PATH}/{order_id}/{_TARGET_LEG}")
            self._log.info("target_leg_retired", order_id=order_id, symbol=symbol)
            return True
        except Exception:
            self._log.error(
                "target_leg_cancel_failed",
                order_id=order_id,
                symbol=symbol,
                exc_info=True,
            )
            return False

    def _live_super_orders(self) -> list[dict[str, Any]]:
        """Today's super orders that are OURS. Raises on transport failure.

        Deliberately NOT fail-soft. Every caller uses this to decide either
        "is this position already protected" or "is there a leg I must retire
        before selling" — and answering "no" on a network blip would stack a
        duplicate stop in the first case and open a naked short in the second.
        An exception makes the caller skip the tick instead of acting on a
        false negative.
        """
        result = self._request("GET", _SUPER_PATH) or []
        if not isinstance(result, list):
            return []
        return [o for o in result if self._owns_super_order(o)]

    def _owns_super_order(self, order: dict[str, Any]) -> bool:
        """Can we PROVE this super order is the bot's? (Decision 027.)

        Two independent proofs, because neither alone is sufficient:

        ``correlationId`` is what we set at placement — but it is UNVERIFIED
        whether Dhan echoes it onto a super order the way it does a plain one,
        and this cannot be established without placing a real order. If it does
        not, this check alone would classify every one of our own super orders
        as the user's: we would never retire a stop leg before selling (naked
        short) and ``stop_coverage`` would read every position as uncovered and
        HALT the bucket each tick.

        So ``owns_order_id`` — a ledger lookup injected by the caller — is the
        fallback that makes ownership survive both a missing correlationId and
        a process restart (an in-memory set would not). When neither proof is
        available we answer False, which is the SAFE direction on a shared
        account: the bot leaves the order alone rather than acting on someone
        else's.
        """
        if _is_ours(order.get("correlationId")):
            return True
        order_id = str(order.get("orderId", ""))
        if order_id and self._owns_order_id is not None:
            if order_id in self._owned_ids:
                return True
            try:
                owned = bool(self._owns_order_id(order_id))
                # Cache only the TRUE answer, and cache it forever: a ledger row
                # for this id cannot later un-exist. A False is never cached —
                # the row appears moments after placement, so a negative is a
                # statement about timing, not about ownership, and remembering
                # it would permanently disown one of our own orders.
                if owned:
                    self._owned_ids.add(order_id)
                return owned
            except Exception:
                self._log.warning(
                    "super_order_ownership_lookup_failed",
                    order_id=order_id,
                    exc_info=True,
                )
        return False

    @staticmethod
    def _stop_leg(order: dict[str, Any]) -> dict[str, Any] | None:
        """The resting STOP_LOSS_LEG of a super order, if it is still live."""
        for leg in order.get("legDetails") or []:
            if str(leg.get("legName", "")).upper() != _STOP_LOSS_LEG:
                continue
            status = str(leg.get("orderStatus", "")).upper()
            # An absent status means the venue did not tell us; treat it as
            # live, because assuming "already gone" is the direction that
            # leaves an orphan leg behind.
            if not status or status in _LEG_LIVE_STATES:
                return leg
            return None
        return None

    def attached_stop_triggers(self) -> dict[str, Decimal]:
        """``symbol → stop price`` for our live super-order stop legs.

        Read by the Decision 022 sweep so it neither stacks a second resting
        stop on a position the venue already protects, nor mistakes the venue's
        own leg for an orphan and cancels it.
        """
        out: dict[str, Decimal] = {}
        for order in self._live_super_orders():
            leg = self._stop_leg(order)
            if leg is None:
                continue
            symbol = order.get("tradingSymbol") or str(order.get("securityId", ""))
            raw = leg.get("stopLossPrice", order.get("stopLossPrice"))
            if not symbol or raw is None:
                continue
            try:
                value = Decimal(str(raw))
            except (ArithmeticError, ValueError):
                continue
            if value > 0:
                out[symbol] = value
        return out

    def _retire_attached_stop(self, symbol: str) -> None:
        """Cancel any resting stop leg on ``symbol`` before we close it.

        RAISES if a leg exists and cannot be cancelled, and if the lookup
        itself fails. Both abort the caller's closing order, which is the
        deliberate direction: an un-retired leg that fires against a position
        we just sold opens a SHORT, whereas a close that does not happen leaves
        a position that is still protected by the very leg we failed to cancel.
        Refusing to sell is recoverable; selling twice is not.
        """
        try:
            orders = self._live_super_orders()
        except Exception as exc:
            raise AttachedStopRetireError(
                f"cannot confirm whether {symbol} has a resting stop leg: {exc}"
            ) from exc

        for order in orders:
            order_symbol = order.get("tradingSymbol") or str(
                order.get("securityId", "")
            )
            if order_symbol != symbol or self._stop_leg(order) is None:
                continue
            order_id = str(order.get("orderId", ""))
            try:
                self._request(
                    "DELETE", f"{_SUPER_PATH}/{order_id}/{_STOP_LOSS_LEG}"
                )
            except Exception as exc:
                raise AttachedStopRetireError(
                    f"{symbol} stop leg {order_id} would outlive the position: "
                    f"{exc}"
                ) from exc
            self._log.info(
                "attached_stop_retired_before_close",
                symbol=symbol,
                order_id=order_id,
            )

    def retire_attached_stop(self, symbol: str) -> None:
        """Public form of the guard, for the sweep's orphan-leg pass."""
        self._retire_attached_stop(symbol)

    def retire_stale_target_legs(self) -> list[str]:
        """Retry target-leg cancels that failed at placement. Returns cleared ids.

        A target leg that survives placement is a live exit at a price no
        backtest justifies (House Rule 7), so the sweep keeps trying rather
        than leaving it to chance.
        """
        cleared: list[str] = []
        for order in self._live_super_orders():
            legs = order.get("legDetails") or []
            live_target = any(
                str(leg.get("legName", "")).upper() == _TARGET_LEG
                and str(leg.get("orderStatus", "")).upper() in _LEG_LIVE_STATES
                for leg in legs
            )
            if not live_target:
                continue
            order_id = str(order.get("orderId", ""))
            symbol = order.get("tradingSymbol") or str(order.get("securityId", ""))
            if order_id and self._retire_target_leg(order_id, symbol):
                cleared.append(order_id)
        return cleared

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

    def get_holdings(self) -> list[PositionInfo]:
        """Settled delivery stock, as positions.

        Dhan splits a delivery trade across two endpoints by TIME, not by kind:

            /v2/positions  "all open positions FOR THE DAY"
            /v2/holdings   "bought/sold in PREVIOUS trading sessions,
                            including T1 and delivered quantities"

        So an MTF buy lives in ``positions`` on its purchase day and moves to
        ``holdings`` from the next session onward. The bot read only positions,
        which meant every swing-indian position — a strategy that holds for DAYS
        by design — became invisible the morning after it opened. On 2026-08-13
        PIIND (15 @ 2514.50) had no stop, no exit, no P&L and no alarm, because
        the reconciler had concluded it was closed. The bucket could enter and
        then never exit.

        ``availableQty`` is the sellable number — Dhan defines it as "quantity
        available for transaction". NOT ``totalQty``, which counts stock pledged
        elsewhere or otherwise unsellable.

        ``entry_price`` here is Dhan's ``avgCostPrice``, defined as the average
        "across full position" — i.e. blended over every purchase INCLUDING the
        user's own on this shared account. It is therefore a fallback only; the
        stop path overrides it with the bot's own ledger entry (see
        ``_load_entry_prices``), because a trigger computed off a blended
        average is a trigger at the wrong price.
        """
        result = self._request("GET", "/v2/holdings") or []
        out: list[PositionInfo] = []
        for h in result:
            qty = Decimal(str(h.get("availableQty", 0) or 0))
            if qty <= 0:
                continue
            out.append(
                PositionInfo(
                    symbol=h.get("tradingSymbol", str(h.get("securityId", ""))),
                    side="long",  # you cannot hold a short in a demat account
                    size=qty,
                    entry_price=Decimal(str(h.get("avgCostPrice", 0) or 0)),
                    raw={**h, "_source": "holdings"},
                )
            )
        return out

    def get_positions(self) -> list[PositionInfo]:
        """Open positions, UNION settled holdings.

        A union is correct rather than a merge because the two endpoints are
        disjoint by Dhan's own definition — today vs previous sessions — so the
        same shares cannot be counted twice. Positions still win on a symbol
        present in both: that is the fresher view, and it carries the side, so a
        same-day re-entry is never mistaken for settled stock.

        Fail-soft on the holdings leg: if it errors we return positions alone,
        which is exactly the pre-2026-08-14 behaviour. Degrading to the old blind
        spot beats failing the whole sweep, which protects nothing at all.
        """
        positions = self._positions_only()
        try:
            holdings = self.get_holdings()
        except Exception:
            self._log.warning("dhan_holdings_fetch_failed", exc_info=True)
            return positions
        seen = {p.symbol for p in positions}
        return positions + [h for h in holdings if h.symbol not in seen]

    def _positions_only(self) -> list[PositionInfo]:
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

    def get_order_charges(
        self, *, start: date, end: date
    ) -> dict[str, OrderCharges]:
        """``orderId → charges``, from Dhan's TRADE HISTORY report.

        Note the endpoint: ``/v2/trades/{from}/{to}/{page}``, which is a
        different resource from the ``/v2/trades`` day book that
        :meth:`get_fills` reads. The day book carries executions and no costs;
        this one carries the per-trade breakdown. Reading the wrong one is why
        every Dhan ``Trade.fees`` in this database has been a hardcoded zero
        since the integration was written, and why every P&L figure downstream —
        dashboard, EOD report, tax ledger, profit factor — has been gross of
        charges.

        Charges arrive per FILL and an order can fill in pieces, so they are
        summed per ``orderId``: one Trade row is one order.

        Paginated, and the page size is not documented, so it walks until a page
        comes back empty rather than assuming one page is everything — a
        truncated read here would silently under-report costs, which is worse
        than not reading them at all.
        """
        out: dict[str, OrderCharges] = {}
        page = 0
        while page < _MAX_CHARGE_PAGES:
            try:
                rows = self._request(
                    "GET",
                    f"/v2/trades/{start:%Y-%m-%d}/{end:%Y-%m-%d}/{page}",
                )
            except DhanAPIError:
                self._log.warning(
                    "trade_charges_fetch_failed", page=page, exc_info=True
                )
                break
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                order_id = str(r.get("orderId", ""))
                if not order_id:
                    continue
                prev = out.get(order_id)
                out[order_id] = OrderCharges(
                    exchange_order_id=order_id,
                    brokerage=_charge(r, "brokerageCharges")
                    + (prev.brokerage if prev else Decimal("0")),
                    stt=_charge(r, "stt") + (prev.stt if prev else Decimal("0")),
                    exchange_txn=_charge(r, "exchangeTransactionCharges")
                    + (prev.exchange_txn if prev else Decimal("0")),
                    sebi=_charge(r, "sebiTax")
                    + (prev.sebi if prev else Decimal("0")),
                    stamp_duty=_charge(r, "stampDuty")
                    + (prev.stamp_duty if prev else Decimal("0")),
                    # Dhan reports GST under its pre-2017 name.
                    gst=_charge(r, "serviceTax")
                    + (prev.gst if prev else Decimal("0")),
                )
            page += 1
        return out

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
            # Dhan has no reduce-only flag for equities, so infer it: a resting
            # order that carries BOTH a trigger price and OUR correlationId is a
            # protective stop this bot placed. ``correlationId`` is only ever
            # set by an API order with a client_order_id, so a stop the USER
            # placed in the app can never match — which is the Decision 027
            # property that matters, since plan_stop_protection CANCELS what it
            # matches here.
            #
            # This was hardcoded False until 2026-08-12, which made
            # ``stops_by_symbol`` permanently empty for Dhan: the sweep could
            # never recognise a stop it had already placed, so it planned a
            # fresh one every 60s. Harmless only while every stop was being
            # rejected — the moment MTF consent let one through, it would have
            # stacked a new resting stop each minute for the life of the
            # position.
            reduce_only=bool(trig) and _is_ours(o.get("correlationId")),
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


def _charge(row: dict[str, Any], key: str) -> Decimal:
    """One charge field as a Decimal; 0 when absent or unparseable.

    Never raises: a single malformed field must not cost us the whole
    charges report, and an under-read is visible (P&L looks too good)
    whereas a crashed sweep is not.
    """
    raw = row.get(key)
    if raw is None:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return Decimal("0")


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
