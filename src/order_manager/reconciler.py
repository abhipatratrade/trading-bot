"""
DB ↔ exchange reconciler.

Runs at startup and every 5 minutes to catch discrepancies between
what the database thinks the exchange state is and what the exchange
actually reports.  Every diff is logged to the ``audit_log`` table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.brokers.base import Broker
from src.core.alerts import send_alert
from src.core.clock import Clock, RealClock
from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BrokerName,
    BucketState,
    OrderSide,
    OrderStatus,
    Position,
    PositionSide,
    Trade,
)
from src.order_manager.ownership import bot_owned_quantities


@dataclass
class ReconcileReport:
    """Summary of what the reconciler found and fixed."""

    positions_updated: int = 0
    positions_closed: int = 0
    orphan_positions: int = 0
    orders_updated: int = 0
    diffs: list[dict[str, Any]] = field(default_factory=list)


class Reconciler:
    """Compares DB state against the live exchange and fixes discrepancies.

    Usage::

        rec = Reconciler(broker, BrokerName.DELTA_INDIA)
        report = rec.run()
    """

    def __init__(
        self,
        broker: Broker,
        broker_name: BrokerName,
        clock: Clock | None = None,
        bucket_ids: list[str] | None = None,
        bucket_fx: dict[str, Decimal] | None = None,
        shared_account: bool = False,
        carry_interest_apr: dict[str, Decimal] | None = None,
    ) -> None:
        self._broker = broker
        self._broker_name = broker_name
        self._clock = clock or RealClock()
        self._log = get_logger("order_manager.reconciler")
        # Decision 027 followup: a SHARED account (Dhan — the Indian buckets
        # share one login with the user's manual trading) holds positions the
        # bot never opened. On such an account the reconciler must ONLY adopt
        # exchange positions the bot itself opened (its own net-long trades),
        # never the user's. Crypto sub-accounts are exclusive (Decision 019),
        # so this stays False there and the whole account is treated as ours.
        self._shared_account = shared_account
        # Decision 019: with one sub-account per bucket, every reconciler is
        # scoped to the bucket(s) on its account so it never sweeps another
        # bucket's rows. ``None`` keeps the legacy broker-wide behaviour
        # (used by single-account smoke scripts / tests).
        self._bucket_ids = bucket_ids
        # Decision 021: bucket_state mirrors the sub-account wallet. fx is
        # the bucket's allocator fx_inr_per_usd (wallet quote → INR) —
        # a FIXED rate per user decision 2026-07-07 (85 for Delta India).
        self._bucket_fx = bucket_fx or {}
        # Decision 032: buckets that carry a broker-FUNDED position overnight
        # (Dhan MTF) pay interest on the funded portion. The backtest does not
        # model it (~4% of net over 24 months), so the bot books it itself when
        # a round-trip closes: bucket_id → annual rate, e.g. 0.146 = 14.6%/yr.
        self._carry_apr = carry_interest_apr or {}
        # symbol → (shortfall qty, consecutive passes seen). See
        # ``_detect_unrecorded_exits``: a position that vanished without a bot
        # order must be observed repeatedly before the bot writes an exit for
        # it, because a single bad read would fabricate one.
        self._shortfall_seen: dict[str, tuple[Decimal, int]] = {}

    def _scope_positions(self) -> list[Any]:
        """Extra WHERE clauses restricting Position rows to this account's buckets."""
        if self._bucket_ids is None:
            return []
        return [Position.bucket_id.in_(self._bucket_ids)]

    def _scope_trades(self) -> list[Any]:
        """Extra WHERE clauses restricting Trade rows to this account's buckets."""
        if self._bucket_ids is None:
            return []
        return [Trade.bucket_id.in_(self._bucket_ids)]

    def run(self) -> ReconcileReport:
        """Full pass: positions, orders, wallet sync, then P&L enrichment."""
        report = ReconcileReport()
        positions = self._reconcile_positions(report)
        try:
            # Runs BEFORE P&L enrichment so a recovered exit pairs with its
            # entry in the same pass, and reuses the positions already fetched
            # above — Dhan is rate-limited and this sweep is on the 5-min loop.
            self._detect_unrecorded_exits(report, positions)
        except Exception:
            self._log.error("unrecorded_exit_detection_failed", exc_info=True)
        self._reconcile_orders(report)
        self._sync_bucket_state()
        try:
            self._sync_wallet_flows()
        except Exception:
            # Observability only — never let it fail the sweep.
            self._log.warning("wallet_flows_sync_failed", exc_info=True)
        try:
            # BEFORE P&L: charges must land first, or the pairing pass would
            # recompute against fees that are still zero and re-stamp
            # pnl_final, undoing the unstamp in the same run.
            self._enrich_trade_charges()
        except Exception:
            self._log.warning("trade_charges_enrichment_failed", exc_info=True)
        try:
            self._enrich_trades_pnl()
        except Exception:
            # P&L enrichment is observability — never let it fail the sweep.
            self._log.warning("pnl_enrichment_failed", exc_info=True)

        if report.diffs:
            self._log.warning(
                "reconcile_diffs_found",
                count=len(report.diffs),
                positions_updated=report.positions_updated,
                orders_updated=report.orders_updated,
            )
            send_alert(
                f"[reconciler] {len(report.diffs)} drift(s) detected: "
                f"{report.positions_updated} pos updated, "
                f"{report.positions_closed} pos closed, "
                f"{report.orphan_positions} orphan(s) imported, "
                f"{report.orders_updated} order(s) updated"
            )
        else:
            self._log.info("reconcile_clean")
        return report

    # ── Broker charges ──────────────────────────────────────────────

    # How far back to look for trades still missing their charges. Generous
    # because charges land at END OF DAY: a trade filled at 15:20 cannot be
    # enriched until that evening at the earliest, and a weekend or a bot
    # outage stretches that further.
    _CHARGES_LOOKBACK_DAYS = 10

    def _enrich_trade_charges(self) -> None:
        """Fill in what the broker actually billed, and re-derive P&L.

        ``Trade.fees`` has been a hardcoded zero on every Dhan trade since the
        integration was written: ``get_fills`` reads ``/v2/trades``, the intraday
        day book, which reports executions and no costs. The costs live on a
        DIFFERENT resource — ``/v2/trades/{from}/{to}/{page}``, the trade-history
        report — which nobody wired up.

        That is not cosmetic. ``realized_pnl`` already subtracts
        ``entry.fees + exit.fees``; it has simply been subtracting nothing. So
        every P&L figure in the dashboard, the EOD report, the tax ledger and the
        edge stats has been GROSS of brokerage, STT, stamp duty, exchange and
        SEBI charges and GST. For swing-indian, whose backtested mean trade is
        ~0.62%, round-trip charges are a large fraction of the edge — which makes
        this the difference between measuring the strategy and flattering it.

        Ordering is the whole difficulty. Fills are known in seconds and charges
        only at end of day, so P&L is inevitably computed first, with zero fees,
        and stamped ``pnl_final``. When charges arrive later this UNSTAMPS the
        round trip — both legs, including the entry's ``closed_by_trade_id``
        pairing mark — so the next ``_enrich_trades_pnl`` pass re-pairs and
        recomputes against real costs. That cannot loop: ``charges_final`` is
        written in the same transaction, so the trade is skipped from then on.
        """
        charges_by_order = None  # fetched lazily; skip the API call if idle
        now = self._clock.now()
        window_start = now - timedelta(days=self._CHARGES_LOOKBACK_DAYS)

        with session_scope() as session:
            candidates = list(
                session.execute(
                    select(Trade).where(
                        Trade.broker == self._broker_name,
                        Trade.exchange_order_id.isnot(None),
                        Trade.status.in_(
                            [OrderStatus.FILLED, OrderStatus.PARTIAL]
                        ),
                        Trade.created_at > window_start,
                        *self._scope_trades(),
                    )
                ).scalars()
            )
            pending = [
                t
                for t in candidates
                if not (t.extra or {}).get("charges_final")
                # A synthetic exit was never an order, so the venue billed
                # nothing against it and there is nothing to look up.
                and not (t.extra or {}).get("synthetic_exit")
            ]
            if not pending:
                return

            charges_by_order = self._broker.get_order_charges(
                start=window_start.date(), end=now.date()
            )
            if not charges_by_order:
                return

            for trade in pending:
                charges = charges_by_order.get(trade.exchange_order_id or "")
                if charges is None:
                    continue
                if not charges_are_billed(charges):
                    continue

                trade.fees = charges.total
                trade.extra = {
                    **(trade.extra or {}),
                    "charges": charges.as_dict(),
                    "charges_final": True,
                }
                self._unstamp_pnl_for_recompute(session, trade)
                self._log.info(
                    "trade_charges_recorded",
                    exchange_order_id=trade.exchange_order_id,
                    symbol=trade.symbol,
                    total=str(charges.total),
                )

    def _unstamp_pnl_for_recompute(self, session: Any, trade: Trade) -> None:
        """Clear a finalised P&L so it recomputes with real fees.

        Both legs must be cleared, and the entry's ``closed_by_trade_id`` with
        them: the pairing loop skips entries already marked closed, so leaving
        it would make the exit permanently unpairable and strand it with no P&L
        at all — worse than the gross figure we are correcting.
        """
        pnl_keys = (
            "pnl_usd",
            "pnl_pct",
            "pnl_kind",
            "pnl_final",
            "pnl_updated_at",
            "carry_interest",
        )
        legs = [trade]
        extra = trade.extra or {}
        partner_id = extra.get("closed_by_trade_id")
        if partner_id:
            partner = session.get(Trade, partner_id)
            if partner:
                legs.append(partner)
        else:
            # This may itself be the exit; find the entry it closed.
            entry = session.execute(
                select(Trade).where(
                    Trade.broker == self._broker_name,
                    Trade.symbol == trade.symbol,
                    Trade.extra["closed_by_trade_id"].astext == str(trade.id),
                )
            ).scalars().first()
            if entry:
                legs.append(entry)

        for leg in legs:
            e = dict(leg.extra or {})
            if not any(k in e for k in pnl_keys):
                continue
            for k in pnl_keys:
                e.pop(k, None)
            e.pop("closed_by_trade_id", None)
            leg.extra = e

    # ── Unrecorded exits ────────────────────────────────────────────

    # Consecutive passes a shortfall must survive before the bot writes an exit
    # it never sent. NOT paranoia: ``DhanClient.get_positions`` fails SOFT on
    # the holdings leg — if ``/v2/holdings`` errors it returns intraday
    # positions alone, which makes every settled swing holding briefly look
    # sold. Acting on one such read would record a fictional exit and make the
    # bot abandon a position it still holds. A transient failure does not
    # survive three passes; a real exit does, and stays detected forever after.
    _SHORTFALL_CONFIRM_PASSES = 3

    def _detect_unrecorded_exits(
        self, report: ReconcileReport, positions: list[Any] | None = None
    ) -> None:
        """Record exits the ACCOUNT took but the bot never placed.

        The bot answers "how many shares are mine?" by adding its BUY rows and
        subtracting its SELL rows (``net_owned``). That only balances while
        every sale of its stock passes through ``OrderManager``. Three things
        break that assumption, and one of them is new:

          * Dhan's MIS auto-square-off (~15:20) closes intraday positions;
          * the user sells, by hand, on this SHARED account;
          * **a Decision 034 stop leg fires** — placed by the venue as part of
            the entry, so no order is ever sent and no row is ever written.

        In all three the shares leave the account and the ledger keeps counting
        them. That ledger is the ONLY thing separating the bot's stock from the
        user's (Decision 027), so a permanent over-count means that the next
        time the user buys the same scrip, the bot treats part of THEIR holding
        as its own — and may rest a stop on it or sell it. The trade also never
        reaches realized P&L, the tax ledger or the EOD report.

        The fix deliberately does NOT try to identify what sold the shares.
        Asking the venue "did the stop leg fire" needs ``GET /v2/super/orders``,
        whose cross-day retention is unverified and unverifiable offline. But
        the bot does not need to know WHAT sold them — only that they are gone,
        which is a comparison between our own ledger and position data this
        sweep already fetches. So this rests on nothing unverified, works for
        all three causes at once, and could have shipped before super orders
        existed. It fixes a hole that is already open today.
        """
        if not self._shared_account or not self._bucket_ids:
            # Crypto sub-accounts are exclusive (Decision 019) and the bot
            # trusts exchange positions directly there, so the Trade ledger
            # drives nothing and an over-count is harmless.
            return

        if positions is None:
            positions = self._broker.get_positions()

        broker_qty: dict[str, Decimal] = {}
        for p in positions:
            if p.side == "long" and p.size > 0:
                broker_qty[p.symbol] = broker_qty.get(
                    p.symbol, Decimal("0")
                ) + p.size

        now = self._clock.now()
        with session_scope() as session:
            ledger = bot_owned_quantities(
                session,
                broker_name=self._broker_name,
                bucket_ids=self._bucket_ids,
                now=now,
            )

        shortfalls: dict[str, Decimal] = {}
        for symbol, owned in ledger.items():
            gap = owned - broker_qty.get(symbol, Decimal("0"))
            if gap > 0:
                shortfalls[symbol] = gap

        confirmed = self._confirm_shortfalls(shortfalls)
        if not confirmed:
            return

        sells = self._todays_sell_fills()
        for symbol, qty in confirmed.items():
            try:
                self._write_unrecorded_exit(
                    symbol, qty, sells.get(symbol, []), report
                )
            except Exception:
                self._log.error(
                    "unrecorded_exit_write_failed",
                    symbol=symbol,
                    qty=str(qty),
                    exc_info=True,
                )
                continue
            self._shortfall_seen.pop(symbol, None)

    def _confirm_shortfalls(
        self, shortfalls: dict[str, Decimal]
    ) -> dict[str, Decimal]:
        """Shortfalls seen the same size for enough passes to act on. PURE-ish.

        A CHANGED size restarts the count rather than advancing it: a position
        being sold down in pieces is still moving, and the right moment to
        record it is once it has settled.
        """
        confirmed: dict[str, Decimal] = {}
        for symbol, qty in shortfalls.items():
            prev_qty, count = self._shortfall_seen.get(symbol, (None, 0))
            count = count + 1 if prev_qty == qty else 1
            self._shortfall_seen[symbol] = (qty, count)
            if count >= self._SHORTFALL_CONFIRM_PASSES:
                confirmed[symbol] = qty
        # A shortfall that healed on its own was a bad read, not an exit.
        for symbol in list(self._shortfall_seen):
            if symbol not in shortfalls:
                self._shortfall_seen.pop(symbol, None)
        return confirmed

    def _todays_sell_fills(self) -> dict[str, list]:
        """``symbol → today's SELL fills``. Empty on any failure."""
        try:
            fills = self._broker.get_fills()
        except Exception:
            self._log.warning("unrecorded_exit_fills_fetch_failed", exc_info=True)
            return {}
        out: dict[str, list] = {}
        for f in fills:
            if str(f.side).lower() == "sell":
                out.setdefault(f.symbol, []).append(f)
        return out

    def _write_unrecorded_exit(
        self,
        symbol: str,
        qty: Decimal,
        sell_fills: list,
        report: ReconcileReport,
    ) -> None:
        """Write the SELL row the bot never sent, and page about it.

        The price is taken from today's SELL fills ONLY when their total
        quantity matches the shortfall exactly. That is a deliberately strict
        rule: on a shared account the trade book also carries the USER's sells,
        and a partial match could not be told apart from theirs. An exit with
        no price still corrects the ledger — which is the part that protects
        the user's stock — and simply does not pair for P&L. A fabricated
        price would corrupt realized P&L and the tax ledger permanently, which
        is far worse than a gap somebody can see.
        """
        # Local imports match this module's existing idiom for cross-module
        # helpers (see ``_enrich_trades_pnl``) and keep the reconciler free of
        # an import-time dependency on the order manager.
        from src.order_manager.manager import make_client_order_id
        from src.order_manager.pnl import aggregate_fills

        now = self._clock.now()
        price: Decimal | None = None
        if sell_fills and sum((f.size for f in sell_fills), Decimal("0")) == qty:
            agg = aggregate_fills(
                [(f.price, f.size, f.commission) for f in sell_fills]
            )
            price = agg.avg_price if agg else None

        with session_scope() as session:
            entry = self._latest_open_entry(session, symbol)
            bucket_id = entry.bucket_id if entry else self._bucket_ids[0]
            strategy_name = entry.strategy_name if entry else None

            # Deterministic and DAY-scoped, so re-running the reconciler cannot
            # duplicate the row. It self-limits too: once written, net_owned
            # drops by qty and the shortfall stops being detected at all.
            client_oid = make_client_order_id(
                bucket_id or "unknown",
                symbol,
                "sell",
                now,
                f"unrecorded-exit-{now.strftime('%Y%m%d')}",
            )
            if session.execute(
                select(Trade.id).where(Trade.client_order_id == client_oid)
            ).first():
                return

            extra: dict[str, Any] = {
                "reduce_only": True,
                "synthetic_exit": True,
                "detected_by": "position_shortfall",
            }
            if price is not None:
                extra["avg_fill_price"] = str(price)

            session.add(
                Trade(
                    strategy_id=bucket_id or "unknown",
                    bucket_id=bucket_id,
                    strategy_name=strategy_name or "unrecorded_exit",
                    broker=self._broker_name,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    quantity=qty,
                    price=price,
                    client_order_id=client_oid,
                    # Never a real venue id — this order was never sent. The
                    # prefix keeps it from ever matching an open-order set or a
                    # get_order lookup.
                    exchange_order_id=f"unrecorded:{symbol}:{now:%Y%m%d%H%M%S}",
                    status=OrderStatus.FILLED,
                    submitted_at=now,
                    filled_at=now,
                    extra=extra,
                )
            )
            session.add(
                AuditLog(
                    strategy_id=bucket_id or "unknown",
                    event_type=AuditEventType.RECONCILE_DIFF,
                    message=f"Unrecorded exit recorded for {symbol}",
                    payload={
                        "symbol": symbol,
                        "quantity": str(qty),
                        "price": str(price) if price is not None else None,
                    },
                )
            )

        report.diffs.append(
            {
                "type": "unrecorded_exit_recorded",
                "symbol": symbol,
                "quantity": str(qty),
                "price": str(price) if price is not None else None,
            }
        )
        self._log.warning(
            "unrecorded_exit_recorded",
            symbol=symbol,
            quantity=str(qty),
            price=str(price) if price is not None else None,
        )
        send_alert(
            f"[reconciler] {symbol}: {qty} share(s) left the account with no "
            f"order from the bot "
            f"({'@ ' + str(price) if price is not None else 'FILL PRICE UNKNOWN'})"
            f" — ledger corrected. Cause: stop leg, auto-square-off, or a "
            f"manual sell."
        )

    def _latest_open_entry(self, session: Any, symbol: str) -> Trade | None:
        """Newest unpaired BUY entry for ``symbol``, for attribution."""
        rows = (
            session.execute(
                select(Trade)
                .where(
                    Trade.broker == self._broker_name,
                    Trade.symbol == symbol,
                    Trade.side == OrderSide.BUY,
                    *self._scope_trades(),
                )
                .order_by(Trade.created_at.desc())
                .limit(50)
            )
            .scalars()
            .all()
        )
        for t in rows:
            extra = t.extra or {}
            if extra.get("reduce_only") or extra.get("closed_by_trade_id"):
                continue
            return t
        return None

    # ── Position reconciliation ─────────────────────────────────────

    def _reconcile_positions(self, report: ReconcileReport) -> list[Any]:
        exchange_positions = self._broker.get_positions()
        exchange_by_symbol: dict[str, Any] = {
            p.symbol: p for p in exchange_positions
        }

        with session_scope() as session:
            # All non-flat DB positions for this broker, scoped to the
            # bucket(s) on this sub-account (Decision 019).
            db_positions = list(
                session.execute(
                    select(Position).where(
                        Position.broker == self._broker_name,
                        Position.side != PositionSide.FLAT,
                        *self._scope_positions(),
                    )
                ).scalars()
            )
            db_symbols = {p.symbol for p in db_positions}

            self._close_unattributed_positions(report, session, exchange_by_symbol)

            # Case 0: a SHORT row on a shared account is not ours, whatever put
            # it there. Flatten it, and do so BEFORE anything else reads it.
            #
            # This is cleanup AND containment. The stop sweep now refuses to
            # protect a short, but a short Position row is also picked up by
            # ``BucketRunner._run_exits``, which would compute the closing side
            # as BUY and try to "close" the phantom by BUYING 15 shares. Exits
            # carry ``allow_when_killed=True`` (Decision 024), so the kill
            # switch would NOT have stopped it — the one path where a stale row
            # could still have opened a real position.
            for db_pos in db_positions:
                if not self._shared_account or db_pos.side != PositionSide.SHORT:
                    continue
                self._log.warning(
                    "short_position_row_flattened",
                    symbol=db_pos.symbol,
                    quantity=str(db_pos.quantity),
                    bucket_id=db_pos.bucket_id,
                )
                db_pos.side = PositionSide.FLAT
                db_pos.quantity = Decimal("0")
                db_pos.closed_at = self._clock.now()
                report.positions_closed += 1
                report.diffs.append(
                    {
                        "type": "short_position_row_flattened",
                        "symbol": db_pos.symbol,
                        "reason": "shorts are never the bot's on a shared account",
                    }
                )
            db_positions = [
                p for p in db_positions if p.side != PositionSide.FLAT
            ]
            db_symbols = {p.symbol for p in db_positions}

            # Case 1: DB has position, exchange doesn't → close it
            for db_pos in db_positions:
                if db_pos.symbol not in exchange_by_symbol:
                    diff = {
                        "type": "position_closed_on_exchange",
                        "symbol": db_pos.symbol,
                        "db_side": db_pos.side.value,
                        "db_size": str(db_pos.quantity),
                    }
                    report.diffs.append(diff)
                    report.positions_closed += 1
                    db_pos.side = PositionSide.FLAT
                    db_pos.quantity = Decimal("0")
                    db_pos.closed_at = self._clock.now()
                    session.add(
                        AuditLog(
                            strategy_id=db_pos.strategy_id,
                            event_type=AuditEventType.RECONCILE_DIFF,
                            message=f"Position {db_pos.symbol} closed on exchange but open in DB",
                            payload=diff,
                        )
                    )
                    self._log.warning("position_closed_on_exchange", **diff)
                    send_alert(
                        f"[reconciler] Position CLOSED externally: {db_pos.symbol} "
                        f"(was {db_pos.side.value} {db_pos.quantity})"
                    )

            # Case 2: Exchange has position, DB has no NON-FLAT row → IMPORT IT.
            # A FLAT row may still exist from a prior close; uq_position_key
            # is on (strategy_id, broker, symbol) so plain INSERT collides.
            # Upsert: reopen the FLAT row if it exists, otherwise insert.
            # Without this, the bot's dedup gate in shared.allocator.sizer
            # never sees the position and keeps placing new orders every
            # tick. Bucket attribution comes from the most-recent filled
            # Trade for the symbol; truly external positions stay
            # unattributed and we just emit a warning.
            # On a shared account, the bot's own net-long holdings by symbol.
            # Anything on the exchange NOT here is the user's manual position
            # and must never be adopted (the 2026-07-22 incident).
            owned = (
                bot_owned_quantities(
                    session,
                    broker_name=self._broker_name,
                    bucket_ids=self._bucket_ids or [],
                    now=self._clock.now(),
                )
                if self._shared_account
                else {}
            )

            for sym, ex_pos in exchange_by_symbol.items():
                if sym in db_symbols:
                    continue
                if self._shared_account and sym not in owned:
                    # Not opened by the bot → the user's. Leave it entirely
                    # alone (no import, no stop, no exit downstream).
                    self._log.info(
                        "external_position_ignored",
                        symbol=sym,
                        exchange_side=ex_pos.side,
                        exchange_size=str(ex_pos.size),
                    )
                    continue
                if self._shared_account and ex_pos.side == "short":
                    # A short is never provably ours here: ``net_owned``
                    # expresses only long quantities, so the check above can
                    # pass on a symbol we are LONG in the ledger while the
                    # broker reports a SHORT — which is precisely what a
                    # settlement artifact looks like.
                    #
                    # On 2026-08-18 selling PIIND out of holdings produced a
                    # negative day-position, and this branch adopted it as a
                    # short Position row (``orphan_position_reopened``). That
                    # row then told the stop sweep there was a short to
                    # protect. Adopting it is how the artifact became state.
                    self._log.warning(
                        "short_position_not_adopted",
                        symbol=sym,
                        exchange_size=str(ex_pos.size),
                    )
                    continue
                latest_trade = self._latest_filled_trade(session, sym)
                strategy_id = latest_trade.strategy_id if latest_trade else "unknown"
                bucket_id = latest_trade.bucket_id if latest_trade else None
                strategy_name = (
                    latest_trade.strategy_name if latest_trade else None
                )
                side = _exchange_side_to_position(ex_pos.side)
                # Never adopt more than the bot's OWN net quantity: if the user
                # also holds this cash symbol, the exchange size is bot+user, so
                # cap at what the bot opened (shared account only).
                import_qty = (
                    min(ex_pos.size, owned[sym])
                    if self._shared_account
                    else ex_pos.size
                )
                if import_qty < ex_pos.size:
                    self._log.warning(
                        "orphan_qty_capped_to_bot_owned",
                        symbol=sym,
                        exchange_size=str(ex_pos.size),
                        bot_owned=str(owned.get(sym)),
                    )
                existing_flat = session.execute(
                    select(Position).where(
                        Position.strategy_id == strategy_id,
                        Position.broker == self._broker_name,
                        Position.symbol == sym,
                    )
                ).scalar_one_or_none()
                diff = {
                    "type": (
                        "orphan_position_reopened"
                        if existing_flat is not None
                        else "orphan_position_imported"
                    ),
                    "symbol": sym,
                    "exchange_side": ex_pos.side,
                    "exchange_size": str(ex_pos.size),
                    "bucket_id": bucket_id,
                    "strategy_name": strategy_name,
                    "strategy_id": strategy_id,
                    "source_trade_id": latest_trade.id if latest_trade else None,
                }
                report.diffs.append(diff)
                report.orphan_positions += 1
                if existing_flat is not None:
                    existing_flat.bucket_id = bucket_id or existing_flat.bucket_id
                    existing_flat.strategy_name = (
                        strategy_name or existing_flat.strategy_name
                    )
                    existing_flat.side = side
                    existing_flat.quantity = import_qty
                    existing_flat.entry_price = ex_pos.entry_price
                    existing_flat.leverage = ex_pos.leverage
                    existing_flat.liquidation_price = ex_pos.liquidation_price
                    existing_flat.opened_at = self._clock.now()
                    existing_flat.closed_at = None
                else:
                    session.add(
                        Position(
                            strategy_id=strategy_id,
                            bucket_id=bucket_id,
                            strategy_name=strategy_name,
                            broker=self._broker_name,
                            symbol=sym,
                            side=side,
                            quantity=import_qty,
                            entry_price=ex_pos.entry_price,
                            leverage=ex_pos.leverage,
                            liquidation_price=ex_pos.liquidation_price,
                            opened_at=self._clock.now(),
                        )
                    )
                session.add(
                    AuditLog(
                        strategy_id=strategy_id,
                        event_type=AuditEventType.RECONCILE_DIFF,
                        message=f"Orphan position {sym} imported from exchange",
                        payload=diff,
                    )
                )
                self._log.warning("orphan_position_imported", **diff)

            # Case 3: Both have position → verify size matches AND backfill
            # bucket_id / strategy_name if they were never set (rows created
            # under the legacy schema, or by an old code path).
            for db_pos in db_positions:
                ex_pos = exchange_by_symbol.get(db_pos.symbol)
                if ex_pos is None:
                    continue

                if db_pos.bucket_id is None or db_pos.strategy_name is None:
                    latest_trade = self._latest_filled_trade(
                        session, db_pos.symbol
                    )
                    if latest_trade is not None:
                        if db_pos.bucket_id is None and latest_trade.bucket_id:
                            db_pos.bucket_id = latest_trade.bucket_id
                        if (
                            db_pos.strategy_name is None
                            and latest_trade.strategy_name
                        ):
                            db_pos.strategy_name = latest_trade.strategy_name

                if abs(db_pos.quantity - ex_pos.size) > Decimal("0.0001"):
                    diff = {
                        "type": "position_size_mismatch",
                        "symbol": db_pos.symbol,
                        "db_size": str(db_pos.quantity),
                        "exchange_size": str(ex_pos.size),
                    }
                    report.diffs.append(diff)
                    report.positions_updated += 1
                    db_pos.quantity = ex_pos.size
                    db_pos.entry_price = ex_pos.entry_price
                    if ex_pos.liquidation_price:
                        db_pos.liquidation_price = ex_pos.liquidation_price
                    session.add(
                        AuditLog(
                            strategy_id=db_pos.strategy_id,
                            event_type=AuditEventType.RECONCILE_DIFF,
                            message=f"Position {db_pos.symbol} size mismatch, updated to exchange",
                            payload=diff,
                        )
                    )
                    self._log.warning("position_size_mismatch", **diff)

        # Handed to ``_detect_unrecorded_exits`` so the account is read once
        # per pass rather than twice — Dhan is rate-limited and shared with the
        # user's manual trading.
        return exchange_positions

    def _close_unattributed_positions(
        self, report: ReconcileReport, session: Any, exchange_by_symbol: dict
    ) -> None:
        """Flatten IMMORTAL ``bucket_id IS NULL`` rows on a shared account.

        The orphan import writes ``bucket_id`` from the most recent filled Trade
        for the symbol, and leaves it NULL when it cannot attribute one. Nothing
        can ever clean those rows up again: every consumer that could close them
        scopes on ``Position.bucket_id.in_(bucket_ids)``, and NULL never matches
        ``IN``. They live forever.

        That looked harmless — exits, the stop sweep, attribution and the
        dashboard all scope by bucket, so none of them can see one. But
        ``eod.py`` reads ``select(Position).where(side != FLAT)`` with NO
        scoping, so a ghost row is reported as a position CARRIED OVERNIGHT.
        On 2026-08-18 that would have claimed 15 PIIND and 238 PPLPHARMA the
        user had not held for days — in the one section of the report you would
        read specifically to check overnight exposure.

        Two conditions, either sufficient, and both mean the row is telling
        nobody anything true:

          * the exchange does not report the symbol at all; or
          * a properly attributed non-flat row already exists for it, making
            this one a duplicate; or
          * the bot's own ledger says it does not own the symbol. Indian
            equity settles T+1, so a scrip SOLD today still shows in
            holdings tonight — and would be reported as carried overnight.

        A NULL row is deliberately NOT flattened when it is the only record of
        something the exchange still reports — that would be destroying the sole
        trace of a live position to tidy a report.
        """
        if not self._shared_account:
            # Only the shared Dhan account produces unattributed rows, and
            # crypto sub-accounts share a broker — a Delta reconciler must not
            # reach across into a sibling sub-account's rows.
            return

        ghosts = list(
            session.execute(
                select(Position).where(
                    Position.broker == self._broker_name,
                    Position.bucket_id.is_(None),
                    Position.side != PositionSide.FLAT,
                )
            ).scalars()
        )
        if not ghosts:
            return

        attributed = {
            p.symbol
            for p in session.execute(
                select(Position).where(
                    Position.broker == self._broker_name,
                    Position.bucket_id.isnot(None),
                    Position.side != PositionSide.FLAT,
                )
            ).scalars()
        }
        owned = bot_owned_quantities(
            session,
            broker_name=self._broker_name,
            bucket_ids=self._bucket_ids or [],
            now=self._clock.now(),
        )

        for ghost in ghosts:
            duplicate = ghost.symbol in attributed
            absent = ghost.symbol not in exchange_by_symbol
            # The exchange may still report a scrip the bot has SOLD: Indian
            # equity settles T+1, so a holding lingers for a day after the sale.
            # PIIND on 2026-08-18 was exactly that — sold at 12:16, still in
            # holdings, and about to be reported as "carried overnight" in a
            # report the user reads to check overnight exposure.
            #
            # An unattributed row the bot does not OWN is not the bot's business
            # by Decision 027, and `foreign_positions` already reports it as the
            # user's. Keeping a Position row for it contradicts that.
            unowned = ghost.symbol not in owned
            if not (duplicate or absent or unowned):
                continue
            diff = {
                "type": "unattributed_position_flattened",
                "symbol": ghost.symbol,
                "quantity": str(ghost.quantity),
                "reason": (
                    "duplicate" if duplicate
                    else "not_on_exchange" if absent
                    else "not_owned_by_bot"
                ),
            }
            ghost.side = PositionSide.FLAT
            ghost.quantity = Decimal("0")
            ghost.closed_at = self._clock.now()
            report.positions_closed += 1
            report.diffs.append(diff)
            session.add(
                AuditLog(
                    strategy_id=ghost.strategy_id or "unknown",
                    event_type=AuditEventType.RECONCILE_DIFF,
                    message=(
                        f"Unattributed position row {ghost.symbol} flattened "
                        f"({diff['reason']})"
                    ),
                    payload=diff,
                )
            )
            self._log.warning("unattributed_position_flattened", **diff)

    def _latest_filled_trade(self, session, symbol: str) -> Trade | None:
        """Most-recent FILLED Trade for this symbol on this broker.

        Used to attribute orphan positions back to the bucket / strategy
        that originally opened them.
        """
        return session.execute(
            select(Trade)
            .where(
                Trade.broker == self._broker_name,
                Trade.symbol == symbol,
                Trade.status == OrderStatus.FILLED,
                *self._scope_trades(),
            )
            .order_by(Trade.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    # ── Bucket capital sync (Decision 021) ──────────────────────────

    def _sync_bucket_state(self) -> None:
        """Mirror the sub-account wallet into ``bucket_state``.

        available_balance_inr ← wallet available × fx
        locked_margin_inr     ← (order_margin + position_margin) × fx

        Only runs when the reconciler is bucket-scoped (Decision 019).
        With one sub-account per bucket the mapping is 1:1; if multiple
        buckets ever share an account this would double-count, so warn.
        """
        if not self._bucket_ids:
            return
        if len(self._bucket_ids) > 1:
            self._log.warning(
                "bucket_state_sync_shared_account",
                buckets=self._bucket_ids,
                note="wallet mirrored into EACH bucket — capital double-counted",
            )
        try:
            balances = self._broker.get_balances()
        except Exception:
            self._log.warning("bucket_state_sync_balance_fetch_failed", exc_info=True)
            return

        available = sum((b.available for b in balances), Decimal("0"))
        locked = sum(
            (b.order_margin + b.position_margin for b in balances),
            Decimal("0"),
        )

        with session_scope() as session:
            for bucket_id in self._bucket_ids:
                state = session.execute(
                    select(BucketState).where(
                        BucketState.bucket_id == bucket_id
                    )
                ).scalar_one_or_none()
                if state is None:
                    self._log.warning(
                        "bucket_state_row_missing", bucket_id=bucket_id
                    )
                    continue
                fx = self._bucket_fx.get(bucket_id, Decimal("1"))
                state.available_balance_inr = available * fx
                state.locked_margin_inr = locked * fx
        self._log.info(
            "bucket_state_synced",
            buckets=self._bucket_ids,
            available=str(available),
            locked=str(locked),
        )

    # ── Wallet deposit/withdrawal totals (dashboard header) ─────────

    def _sync_wallet_flows(self) -> None:
        """Cache lifetime deposit/withdrawal totals into ``bucket_state``.

        The dashboard (Railway) can't call Delta directly (IP whitelist),
        so the bot stores ``wallet_deposits_usd`` / ``wallet_withdrawals_usd``
        in ``bucket_state.extra`` every sweep. Settlement currency (USD);
        the dashboard converts to INR at the bucket's fixed fx.
        """
        if not self._bucket_ids:
            return
        totals = self._broker.wallet_flow_totals()
        if totals is None:
            return
        deposits, withdrawals = totals
        with session_scope() as session:
            for bucket_id in self._bucket_ids:
                state = session.execute(
                    select(BucketState).where(
                        BucketState.bucket_id == bucket_id
                    )
                ).scalar_one_or_none()
                if state is None:
                    continue
                state.extra = {
                    **(state.extra or {}),
                    "wallet_deposits_usd": str(deposits),
                    "wallet_withdrawals_usd": str(withdrawals),
                    "wallet_flows_updated_at": self._clock.now().isoformat(),
                }

    # ── Per-trade P&L enrichment (Phase 1c) ─────────────────────────

    def _enrich_trades_pnl(self) -> None:
        """Record fills, fees, traded notional, and P&L on recent trades.

        Runs every sweep (5 min by default). Writes to ``Trade.fees`` and
        ``Trade.extra``:

            avg_fill_price, filled_size, contract_size,
            traded_notional_usd, pnl_usd, pnl_pct,
            pnl_kind ("unrealized" | "realized"), pnl_final (bool),
            pnl_updated_at

        Realized P&L pairs a reduce-only exit with the latest prior entry
        for the same (bucket, strategy, symbol) — matches the Phase-1
        one-entry/one-exit flow; partial closes are approximated by the
        smaller of the two sizes. Unrealized P&L for open entries comes
        straight from the exchange's per-position ``unrealized_pnl``.
        ``pnl_pct`` is against traded notional (× leverage for the
        margin-relative return).
        """
        from src.order_manager.pnl import (
            aggregate_fills,
            carry_interest,
            pnl_pct,
            realized_pnl,
            trade_notional,
        )

        now = self._clock.now()
        window_start = now - timedelta(days=7)

        with session_scope() as session:
            candidates = list(
                session.execute(
                    select(Trade).where(
                        Trade.broker == self._broker_name,
                        Trade.exchange_order_id.isnot(None),
                        Trade.status.in_(
                            [
                                OrderStatus.FILLED,
                                OrderStatus.PARTIAL,
                                OrderStatus.OPEN,
                            ]
                        ),
                        Trade.created_at > window_start,
                        *self._scope_trades(),
                    )
                ).scalars()
            )
        open_for_enrich = [
            t for t in candidates if not (t.extra or {}).get("pnl_final")
        ]
        if not open_for_enrich:
            return

        # One fills call covers every candidate order.
        oldest = min(t.created_at for t in open_for_enrich)
        fills = self._broker.get_fills(start_time=oldest)
        by_order: dict[str, list] = {}
        for f in fills:
            by_order.setdefault(f.exchange_order_id, []).append(f)

        exchange_positions = {
            p.symbol: p for p in self._broker.get_positions()
        }

        with session_scope() as session:
            # Re-load inside the write session.
            rows = {
                t.id: t
                for t in session.execute(
                    select(Trade).where(
                        Trade.id.in_([t.id for t in candidates])
                    )
                ).scalars()
            }

            # Pass 1: attach fill aggregates.
            for trade in rows.values():
                if (trade.extra or {}).get("pnl_final"):
                    continue
                order_fills = _fills_for_trade(
                    by_order.get(trade.exchange_order_id or ""), trade
                )
                if not order_fills:
                    continue
                agg = aggregate_fills(
                    [(f.price, f.size, f.commission) for f in order_fills]
                )
                if agg is None:
                    continue
                csize = self._broker.contract_size(trade.symbol)
                notional = trade_notional(
                    agg.avg_price, agg.filled_size, csize
                )
                trade.fees = agg.commission
                trade.extra = {
                    **(trade.extra or {}),
                    "avg_fill_price": str(agg.avg_price),
                    "filled_size": str(agg.filled_size),
                    "contract_size": str(csize),
                    "traded_notional_usd": str(notional),
                }
                if trade.price is None:
                    trade.price = agg.avg_price

            # Pass 2: realized P&L — pair exits with their entries.
            entries = sorted(
                (
                    t
                    for t in rows.values()
                    if not (t.extra or {}).get("reduce_only")
                    and (t.extra or {}).get("avg_fill_price")
                ),
                key=lambda t: t.created_at,
            )
            exits = sorted(
                (
                    t
                    for t in rows.values()
                    if (t.extra or {}).get("reduce_only")
                    and (t.extra or {}).get("avg_fill_price")
                    and not (t.extra or {}).get("pnl_final")
                ),
                key=lambda t: t.created_at,
            )
            for exit_trade in exits:
                # Pair on (bucket, symbol, opposite side) — NOT
                # strategy_name: breaker flatten and protective-stop
                # exits carry a different strategy_name than the entry,
                # and one bucket holds at most one position per symbol
                # (dedup gate), so bucket+symbol is unambiguous.
                entry = next(
                    (
                        e
                        for e in reversed(entries)
                        if e.bucket_id == exit_trade.bucket_id
                        and e.symbol == exit_trade.symbol
                        and e.side != exit_trade.side
                        and e.created_at < exit_trade.created_at
                        and not (e.extra or {}).get("closed_by_trade_id")
                    ),
                    None,
                )
                if entry is None:
                    # Entry may have aged out of the 7-day enrichment
                    # window (position held longer than a week) — fall
                    # back to a direct lookup.
                    entry = self._lookup_entry_for_exit(session, exit_trade)
                if entry is None:
                    continue
                e_extra, x_extra = entry.extra or {}, exit_trade.extra or {}
                entry_avg = Decimal(e_extra["avg_fill_price"])
                exit_avg = Decimal(x_extra["avg_fill_price"])
                entry_size = _filled_size(entry)
                exit_size = _filled_size(exit_trade)
                if entry_size is None or exit_size is None:
                    continue
                size = min(entry_size, exit_size)
                csize = Decimal(x_extra.get("contract_size", "1"))
                pnl = realized_pnl(
                    entry_avg=entry_avg,
                    exit_avg=exit_avg,
                    size=size,
                    contract_size=csize,
                    entry_is_long=(entry.side == OrderSide.BUY),
                    total_fees=(entry.fees or Decimal("0"))
                    + (exit_trade.fees or Decimal("0")),
                )
                notional = Decimal(
                    e_extra.get("traded_notional_usd", "0")
                )
                # Broker-funded carry (Dhan MTF, Decision 032). Charged on the
                # FUNDED portion only — notional minus the own-capital margin
                # the sizer allotted — for every calendar day the position was
                # held. A trade whose bucket has no rate, or whose entry
                # predates the margin stamp, is charged nothing.
                interest = carry_interest(
                    notional=notional,
                    margin=_decimal_or_none(e_extra.get("margin_inr")),
                    annual_rate=self._carry_apr.get(exit_trade.bucket_id or ""),
                    days=_calendar_days(entry.created_at, exit_trade.created_at),
                )
                pnl -= interest
                pct = pnl_pct(pnl, notional)
                stamp = {
                    "pnl_usd": str(pnl),
                    "pnl_pct": str(pct) if pct is not None else None,
                    "pnl_kind": "realized",
                    "pnl_final": True,
                    "pnl_updated_at": now.isoformat(),
                }
                if interest > 0:
                    stamp["carry_interest"] = str(interest)
                exit_trade.extra = {**x_extra, **stamp}
                entry.extra = {
                    **e_extra,
                    **stamp,
                    "closed_by_trade_id": exit_trade.id,
                }

            # Pass 3: unrealized P&L for entries still open on the exchange.
            for entry in entries:
                extra = entry.extra or {}
                if extra.get("pnl_final"):
                    continue
                pos = exchange_positions.get(entry.symbol)
                if pos is None or pos.unrealized_pnl is None:
                    continue
                notional = Decimal(extra.get("traded_notional_usd", "0"))
                pct = pnl_pct(pos.unrealized_pnl, notional)
                entry.extra = {
                    **extra,
                    "pnl_usd": str(pos.unrealized_pnl),
                    "pnl_pct": str(pct) if pct is not None else None,
                    "pnl_kind": "unrealized",
                    "pnl_updated_at": now.isoformat(),
                }

        self._log.info(
            "pnl_enrichment_done",
            trades=len(open_for_enrich),
            fills=len(fills),
        )

    def _lookup_entry_for_exit(self, session, exit_trade: Trade) -> Trade | None:
        """Latest unpaired opposite-side FILLED entry for an exit's symbol.

        Used when the entry predates the 7-day enrichment window (held
        longer than a week). Must already carry ``avg_fill_price`` from
        the sweep that ran while it was in-window.
        """
        candidates = session.execute(
            select(Trade)
            .where(
                Trade.broker == self._broker_name,
                Trade.bucket_id == exit_trade.bucket_id,
                Trade.symbol == exit_trade.symbol,
                Trade.side != exit_trade.side,
                Trade.status == OrderStatus.FILLED,
                Trade.created_at < exit_trade.created_at,
            )
            .order_by(Trade.created_at.desc())
            .limit(10)
        ).scalars()
        for e in candidates:
            extra = e.extra or {}
            if (
                not extra.get("reduce_only")
                and extra.get("avg_fill_price")
                and not extra.get("closed_by_trade_id")
            ):
                return e
        return None

    # ── Order reconciliation ────────────────────────────────────────

    def _reconcile_orders(self, report: ReconcileReport) -> None:
        exchange_open = self._broker.get_open_orders()
        # Keyed on (id, side), not id alone. A Dhan Super Order's legs SHARE one
        # orderId (Decision 034), and the resting STOP_LOSS_LEG stays in an open
        # state for the whole life of the position — so an id-only set would
        # report the filled BUY entry as "still open at the exchange" until the
        # position closed, and the loop below would `continue` past it every
        # time. The entry Trade would never leave OPEN.
        #
        # That is not cosmetic. FILLED is the gate on realized-P&L pairing
        # (``_lookup_entry_for_exit``), the tax ledger, the EOD round-trip
        # stats and the dashboard status. The entry would silently drop out of
        # all four. Side separates the legs cleanly: the entry is a BUY, its
        # protective leg is a SELL.
        open_keys = {
            (o.exchange_order_id, str(o.side).lower()) for o in exchange_open
        }

        with session_scope() as session:
            pending_trades = list(
                session.execute(
                    select(Trade).where(
                        Trade.broker == self._broker_name,
                        Trade.status.in_([
                            OrderStatus.PENDING,
                            OrderStatus.OPEN,
                        ]),
                        *self._scope_trades(),
                    )
                ).scalars()
            )

            for trade in pending_trades:
                still_open = (
                    trade.exchange_order_id,
                    trade.side.value.lower(),
                ) in open_keys
                if trade.exchange_order_id and still_open:
                    if trade.status == OrderStatus.PENDING:
                        trade.status = OrderStatus.OPEN
                        report.orders_updated += 1
                    continue

                # Order is no longer open — check what happened
                new_status = OrderStatus.UNKNOWN
                if trade.exchange_order_id:
                    order_info = self._broker.get_order(trade.exchange_order_id)
                    if order_info:
                        new_status = _map_status(order_info.status)

                if new_status == trade.status:
                    continue

                diff = {
                    "type": "order_status_changed",
                    "exchange_order_id": trade.exchange_order_id,
                    "client_order_id": trade.client_order_id,
                    "symbol": trade.symbol,
                    "old_status": trade.status.value,
                    "new_status": new_status.value,
                }
                report.diffs.append(diff)
                report.orders_updated += 1
                trade.status = new_status
                if new_status == OrderStatus.FILLED:
                    trade.filled_at = self._clock.now()
                session.add(
                    AuditLog(
                        strategy_id=trade.strategy_id,
                        event_type=AuditEventType.RECONCILE_DIFF,
                        message=f"Order {trade.exchange_order_id} status changed",
                        payload=diff,
                    )
                )
                self._log.info("order_status_updated", **diff)


def charges_are_billed(charges: Any) -> bool:
    """True when the venue has actually billed this order. PURE.

    A zero total means "not computed yet", NOT "free". Brokers compute charges
    at end of day, and STT alone is non-zero on both legs of an Indian delivery
    trade — so an all-zero report is the absence of a bill, not a free trade.

    The distinction is the whole point. Accepting a zero would stamp
    ``charges_final`` and bake the placeholder in permanently, which is exactly
    the bug this enrichment exists to fix. Rejecting it costs one more pass.
    """
    return charges is not None and charges.total > 0


def _fills_for_trade(fills: list | None, trade: Trade) -> list:
    """The fills on ``trade``'s order that belong to THIS trade's side. PURE.

    Grouping by ``exchange_order_id`` alone was safe only while one exchange
    order meant one logical order. A Dhan Super Order (Decision 034) breaks
    that: entry, target and stop-loss legs SHARE a single ``orderId``, so when
    the stop leg fires, its SELL fill lands in the same bucket as the entry's
    BUY fill. Averaging them together would hand the entry Trade a blended
    ``avg_fill_price`` sitting between the buy and the stop — a number that
    describes no trade that ever happened, and one that then flows into
    realized P&L, the tax ledger and the EOD report without ever looking
    obviously wrong.

    A fill whose side the venue did not report is KEPT rather than dropped: for
    every non-super order the id match is already conclusive, and discarding
    those would silently disable P&L enrichment on any broker with a sparse
    trade book. The filter only ever needs to separate legs that genuinely
    disagree about direction.
    """
    if not fills:
        return []
    want = trade.side.value.lower()
    return [f for f in fills if not f.side or str(f.side).lower() == want]


def _decimal_or_none(raw: object) -> Decimal | None:
    """Parse a JSONB-stored number, or None if absent/unparseable."""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None


def _filled_size(trade: Trade) -> Decimal | None:
    """How many units of ``trade`` actually traded, or None if unknowable.

    Pass 1 stamps ``filled_size`` from the venue's fill book, but a synthetic
    exit never carries one: it is written for a position that vanished from
    the broker with no order of ours behind it (``synthetic_exit``), so there
    are no fills to aggregate. Its ``quantity`` column IS the size — the
    shortfall the reconciler measured.

    Reading ``extra["filled_size"]`` directly used to raise KeyError on those
    rows, and since the exception escaped the whole Pass 2 loop, ONE synthetic
    exit silently zeroed realized P&L for every trade in the sweep.
    """
    size = _decimal_or_none((trade.extra or {}).get("filled_size"))
    if size is None:
        size = _decimal_or_none(trade.quantity)
    return size if size is not None and size > 0 else None


def _calendar_days(start: Any, end: Any) -> int:
    """Calendar days a position was carried (0 when same-day or unknown).

    MTF funding is charged per calendar day held, weekends included — the
    broker's money is out over the weekend too.
    """
    if start is None or end is None:
        return 0
    return max(0, (end.date() - start.date()).days)


def _map_status(status_str: str) -> OrderStatus:
    return {
        "open": OrderStatus.OPEN,
        "pending": OrderStatus.PENDING,
        "filled": OrderStatus.FILLED,
        "partial": OrderStatus.PARTIAL,
        "canceled": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED,
    }.get(status_str, OrderStatus.UNKNOWN)


def _exchange_side_to_position(side: str) -> PositionSide:
    """Map the broker's free-form side string to our enum.

    Delta India and most perp venues report ``"long"`` / ``"short"`` /
    ``"flat"``; some return ``"buy"`` / ``"sell"`` from the order side.
    Anything we can't classify becomes FLAT so we don't silently
    misattribute a position.
    """
    s = (side or "").lower()
    if s in ("long", "buy"):
        return PositionSide.LONG
    if s in ("short", "sell"):
        return PositionSide.SHORT
    return PositionSide.FLAT
