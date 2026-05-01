"""
ORM models — the persistent state of the trading system.

All tables share a few conventions:
- ``created_at`` / ``updated_at`` on every row, server-side defaults.
- Decimal for any money/quantity/price field; never float.
- JSONB for flexible payloads (audit_log.payload, scanner_snapshot.metrics).
- ``client_order_id`` is the broker-facing idempotency token; UNIQUE.

Tables:
    kill_switch           — global + per-strategy halt flags
    audit_log             — every decision the bot makes
    trade                 — every fill or order intent
    position              — currently-open positions
    strategy_param_change — every load of a strategy's policy.yaml
    daily_universe        — selected symbols per day per strategy
    scanner_snapshot      — full scanner evaluation row per symbol per day
    symbol_mapping        — Delta India ↔ Binance ↔ Kite symbol crosswalk
"""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class BrokerName(StrEnum):
    DELTA_INDIA = "delta_india"
    ZERODHA = "zerodha"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class OrderStatus(StrEnum):
    PENDING = "pending"          # submitted to broker, ack'd
    OPEN = "open"                # resting on book
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"          # set by reconciler when broker state ambiguous


class KillSwitchScope(StrEnum):
    GLOBAL = "global"
    STRATEGY = "strategy"


class AuditEventType(StrEnum):
    # Lifecycle
    BOT_STARTUP = "bot_startup"
    BOT_SHUTDOWN = "bot_shutdown"
    PARAMS_LOADED = "params_loaded"
    RECONCILE_DIFF = "reconcile_diff"

    # Trading decisions
    SCANNER_RUN = "scanner_run"
    UNIVERSE_CHANGE = "universe_change"
    REGIME_CHANGE = "regime_change"
    ORDER_PLACED = "order_placed"
    ORDER_CANCELED = "order_canceled"
    ORDER_FILLED = "order_filled"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"

    # Safety
    BREAKER_TRIPPED = "breaker_tripped"
    KILL_SWITCH_FLIPPED = "kill_switch_flipped"
    DRIFT_ALERT = "drift_alert"


# Common decimal type — 28 digits total, 12 after the point.
# Enough for satoshi-scale crypto prices and notional sizes.
Money = Numeric(28, 12)


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
class KillSwitch(Base, TimestampMixin):
    """Halt control. Checked every loop iteration.

    A row exists per scope. ``GLOBAL`` halts everything; ``STRATEGY`` halts
    one strategy. The dashboard flips ``engaged`` via a single button.
    """

    __tablename__ = "kill_switch"
    __table_args__ = (
        UniqueConstraint("scope", "strategy_id", name="uq_killswitch_scope"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope: Mapped[KillSwitchScope] = mapped_column(
        SAEnum(KillSwitchScope, name="kill_switch_scope"), nullable=False
    )
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engaged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    engaged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
class AuditLog(Base):
    """Append-only event log. Every decision the bot makes lands here."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_ts", "ts"),
        Index("ix_audit_log_strategy_ts", "strategy_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[AuditEventType] = mapped_column(
        SAEnum(AuditEventType, name="audit_event_type"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# Trades & positions
# ---------------------------------------------------------------------------
class Trade(Base, TimestampMixin):
    """One row per fill (or per order intent if not yet filled).

    ``client_order_id`` is generated deterministically by the order manager
    (e.g. ``hash(strategy_id, symbol, side, intent_ts_minute)``) so retries
    cannot double-fire.
    """

    __tablename__ = "trade"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_trade_client_order_id"),
        Index("ix_trade_strategy_symbol", "strategy_id", "symbol"),
        Index("ix_trade_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(
        SAEnum(BrokerName, name="broker_name"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[OrderSide] = mapped_column(
        SAEnum(OrderSide, name="order_side"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    fees: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)

    client_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.PENDING,
    )

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Position(Base, TimestampMixin):
    """Currently-open position per (strategy, broker, symbol).

    Reconciler keeps this in sync with the exchange. When ``side=FLAT`` the
    row stays for history; quantity is 0.
    """

    __tablename__ = "position"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id", "broker", "symbol", name="uq_position_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(
        SAEnum(BrokerName, name="broker_name"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[PositionSide] = mapped_column(
        SAEnum(PositionSide, name="position_side"),
        nullable=False,
        default=PositionSide.FLAT,
    )
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    entry_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    liquidation_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# Strategy parameter changes
# ---------------------------------------------------------------------------
class StrategyParamChange(Base):
    """Records every load of a strategy's policy.yaml.

    Every commit that bumps ``version`` in a YAML file produces a row here
    on the next bot startup. P&L correlations against param shifts come
    from joining this table to ``trade``.
    """

    __tablename__ = "strategy_param_change"
    __table_args__ = (
        Index("ix_param_change_strategy_ts", "strategy_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prior_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backtest_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_yaml: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Scanner output
# ---------------------------------------------------------------------------
class DailyUniverse(Base, TimestampMixin):
    """The lean read-side of the scanner: today's chosen N symbols per strategy.

    The bot's strategy runner reads ONLY this table to know what to trade.
    """

    __tablename__ = "daily_universe"
    __table_args__ = (
        UniqueConstraint(
            "date", "strategy_id", "symbol", name="uq_daily_universe_key"
        ),
        Index("ix_daily_universe_strategy_date", "strategy_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[date_] = mapped_column(Date, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    target_weight: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScannerSnapshot(Base):
    """The full audit row: every coin the scanner evaluated, with metrics and
    filter results. Lets you debug "why did/didn't symbol X get picked".
    """

    __tablename__ = "scanner_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "date", "strategy_id", "symbol", name="uq_scanner_snapshot_key"
        ),
        Index("ix_scanner_snapshot_strategy_date", "strategy_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    date: Mapped[date_] = mapped_column(Date, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    filter_results: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rank_score: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)


# ---------------------------------------------------------------------------
# Symbol mapping (Delta India ↔ Binance ↔ Kite)
# ---------------------------------------------------------------------------
class SymbolMapping(Base, TimestampMixin):
    """Crosswalk between broker-specific symbol names.

    Scanner uses ``listed_on_delta AND listed_on_binance`` as a Phase-1 filter
    for the crypto strategies (so we always have a Binance signal feed for
    anything we trade on Delta).
    """

    __tablename__ = "symbol_mapping"
    __table_args__ = (
        UniqueConstraint("canonical_symbol", name="uq_symbol_mapping_canonical"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    binance_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delta_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kite_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listed_on_binance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    listed_on_delta: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    listed_on_kite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# Re-export for convenience: ``from src.core.models import *`` grabs everything.
__all__ = [
    "BrokerName",
    "OrderSide",
    "PositionSide",
    "OrderStatus",
    "KillSwitchScope",
    "AuditEventType",
    "KillSwitch",
    "AuditLog",
    "Trade",
    "Position",
    "StrategyParamChange",
    "DailyUniverse",
    "ScannerSnapshot",
    "SymbolMapping",
]
