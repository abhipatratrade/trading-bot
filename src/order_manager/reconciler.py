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
        self._reconcile_positions(report)
        self._reconcile_orders(report)
        self._sync_bucket_state()
        try:
            self._sync_wallet_flows()
        except Exception:
            # Observability only — never let it fail the sweep.
            self._log.warning("wallet_flows_sync_failed", exc_info=True)
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

    # ── Position reconciliation ─────────────────────────────────────

    def _reconcile_positions(self, report: ReconcileReport) -> None:
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
                order_fills = by_order.get(trade.exchange_order_id or "")
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
                size = min(
                    Decimal(e_extra["filled_size"]),
                    Decimal(x_extra["filled_size"]),
                )
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
                pct = pnl_pct(pnl, notional)
                stamp = {
                    "pnl_usd": str(pnl),
                    "pnl_pct": str(pct) if pct is not None else None,
                    "pnl_kind": "realized",
                    "pnl_final": True,
                    "pnl_updated_at": now.isoformat(),
                }
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
        open_ids = {o.exchange_order_id for o in exchange_open}

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
                if trade.exchange_order_id and trade.exchange_order_id in open_ids:
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
