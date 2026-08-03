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
    symbol_mapping        — Delta India ↔ Binance ↔ Dhan symbol crosswalk
    regime_model          — persisted HMM artifact per bucket
    regime_snapshot       — per-bucket regime predictions over time
    sizing_snapshot       — every allocator decision (placed or skipped)
    bucket_state          — per-bucket capital & available balance
    daily_equity_anchor   — start-of-day equity per sub-account (DD breaker)
    heartbeat             — liveness row per service (dead-man's switch)
    dhan_token            — shared single-session Dhan access token per client id
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
    Float,
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
    ZERODHA = "zerodha"   # legacy; Indian stocks now use DHAN (Decision 012)
    DHAN = "dhan"


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
    REGIME_MODEL_RETRAINED = "regime_model_retrained"
    REGIME_MODEL_REJECTED = "regime_model_rejected"
    SIZING_DECISION = "sizing_decision"
    STRATEGY_GATE_BLOCKED = "strategy_gate_blocked"
    ORDER_PLACED = "order_placed"
    ORDER_CANCELED = "order_canceled"
    ORDER_FILLED = "order_filled"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"

    # Safety
    BREAKER_TRIPPED = "breaker_tripped"
    KILL_SWITCH_FLIPPED = "kill_switch_flipped"
    DRIFT_ALERT = "drift_alert"
    # A session invariant (Decision 033) failed. Written when the violation is
    # first seen and again whenever what it says changes — NOT every tick, or a
    # steady-state violation would bury the log the way it once buried Telegram.
    # Until this existed an invariant left no durable trace unless it escalated
    # to HALT (which writes KILL_SWITCH_FLIPPED), so the EOD journal reported
    # "nothing tripped" for violations it simply could not see.
    INVARIANT_VIOLATED = "invariant_violated"


class MarketRegime(StrEnum):
    """Three-state HMM regime label (Decision 014)."""

    BEAR = "bear"
    NEUTRAL = "neutral"
    BULL = "bull"


class SizingDecision(StrEnum):
    """Outcome of an allocator call. One row per (strategy, symbol) attempt."""

    PLACED = "placed"
    SKIPPED_INSUFFICIENT = "skipped_insufficient"  # Kelly notional > available balance
    SKIPPED_DEDUP = "skipped_dedup"                # (strategy, tf, symbol) already open
    SKIPPED_NEGATIVE_EDGE = "skipped_negative_edge"  # μ ≤ 0 → Kelly = 0
    SKIPPED_REGIME_GATE = "skipped_regime_gate"    # current regime ∉ strategy's allowed slots
    SKIPPED_SCANNER = "skipped_scanner"            # symbol fell out of universe filter
    SKIPPED_OTHER = "skipped_other"


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
        Index("ix_trade_bucket_symbol", "bucket_id", "symbol"),
        Index("ix_trade_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # New bucket fields (Decision 013): bucket_id is the (type × market) id,
    # strategy_name is the file-name of the strategy that placed this trade.
    bucket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        Index("ix_position_bucket_symbol", "bucket_id", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # New bucket fields (Decision 013). The dedup gate uses
    # (bucket_id, strategy_name, tf, symbol) to decide whether to skip a new
    # entry. tf is read from the strategy_master CSV at runtime; we don't
    # persist it on Position to avoid duplication.
    bucket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
# Which bar a scanner row is ABOUT, as "<IST date>#<bin>" for an intraday cut
# and the plain ISO date for a once-a-day screen.
#
# Both scanner tables are written delete-then-insert, and the key used to be
# (date, strategy_id, symbol) — one row per symbol per DAY. That is right for a
# screen that runs once, and wrong for swing-indian's meanrev cut, which runs on
# every completed 1h bin: each of the 7 passes wiped the previous one, so six
# sevenths of the evidence never survived the session. Worst hit was the 09:16
# pass, which reads the PREVIOUS session's 15:15→15:30 stub — the only path to
# the entry the backtest takes 3 times in 214 trades, and it left no per-symbol
# trace at all. Adding the bar to the key is what makes "why wasn't X picked at
# 11:15?" answerable, which was the whole point of writing a row per evaluated
# symbol (Decision 033).
#
# Not nullable: Postgres treats NULLs as distinct in a UNIQUE constraint, so a
# nullable column would quietly disable the duplicate guard for every
# once-a-day scanner. The ISO date is the honest value there — for a daily
# screen the bar IS the day.
_BAR_KEY_DOC = "Bar this row is about: '<date>#<bin>' intraday, ISO date daily."


class DailyUniverse(Base, TimestampMixin):
    """The lean read-side of the scanner: today's chosen N symbols per strategy.

    The bot's strategy runner reads ONLY this table to know what to trade.
    """

    __tablename__ = "daily_universe"
    __table_args__ = (
        UniqueConstraint(
            "date", "strategy_id", "symbol", "bar_key", name="uq_daily_universe_key"
        ),
        Index("ix_daily_universe_strategy_date", "strategy_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[date_] = mapped_column(Date, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    bar_key: Mapped[str] = mapped_column(
        String(32), nullable=False, doc=_BAR_KEY_DOC
    )
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
            "date", "strategy_id", "symbol", "bar_key", name="uq_scanner_snapshot_key"
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
    bar_key: Mapped[str] = mapped_column(
        String(32), nullable=False, doc=_BAR_KEY_DOC
    )
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


# ---------------------------------------------------------------------------
# Regime (Brain) — Decision 014
# ---------------------------------------------------------------------------
class RegimeModel(Base, TimestampMixin):
    """Persisted HMM artifact per (bucket × symbol).

    The model_blob JSONB column stores the serialised HMM params produced by
    ``RegimeModel.to_dict()`` — start probabilities, transition matrix,
    means, covariances, label mapping. Avoids pickle and a binary store.

    ``symbol`` is one row per coin the bot trades in the bucket. The
    reserved sentinel ``symbol='_market_'`` holds the broad-market BTC
    model used as a fallback for coins with too little history to train
    their own (see ``shared/regime/retrain_job.py``).
    """

    __tablename__ = "regime_model"
    __table_args__ = (
        UniqueConstraint(
            "bucket_id", "symbol", "version", name="uq_regime_model_key"
        ),
        Index(
            "ix_regime_model_bucket_symbol_trained",
            "bucket_id",
            "symbol",
            "trained_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    trained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    n_states: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_columns: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_blob: Mapped[dict] = mapped_column(JSONB, nullable=False)
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class RegimeSnapshot(Base):
    """Per-(bucket, symbol) regime prediction over time.

    One row per Brain inference. ``symbol='_market_'`` rows track the
    broad-market BTC regime separately so it can be used as a fallback
    for symbols whose per-coin model is missing or too low-data.
    """

    __tablename__ = "regime_snapshot"
    __table_args__ = (
        Index(
            "ix_regime_snapshot_bucket_symbol_ts",
            "bucket_id",
            "symbol",
            "ts",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    bucket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    regime: Mapped[MarketRegime] = mapped_column(
        SAEnum(MarketRegime, name="market_regime"), nullable=False
    )
    state_probabilities: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Continuous regime conviction P(bull) − P(bear) ∈ [-1, 1] (Markov 2.0).
    # Nullable: rows written before this column existed have NULL. Downstream
    # sizing/gating still keys on ``regime``; this is observability only.
    signal: Mapped[float | None] = mapped_column(Float, nullable=True)


# ---------------------------------------------------------------------------
# Sizing (Allocator) — Decision 015
# ---------------------------------------------------------------------------
class SizingSnapshot(Base):
    """One audit row per allocator call (placed or skipped).

    Captures the *full* sizing decision: Kelly inputs, bucket capital,
    regime multiplier, available balance, outcome. This is the forensic
    record for "why didn't strategy X take a trade today?".
    """

    __tablename__ = "sizing_snapshot"
    __table_args__ = (
        Index(
            "ix_sizing_snapshot_bucket_strategy_ts",
            "bucket_id",
            "strategy_name",
            "ts",
        ),
        Index("ix_sizing_snapshot_decision", "decision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    bucket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    regime: Mapped[MarketRegime | None] = mapped_column(
        SAEnum(MarketRegime, name="market_regime"), nullable=True
    )
    regime_multiplier: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    fractional_kelly: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    kelly_inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    suggested_notional_inr: Mapped[Decimal | None] = mapped_column(
        Money, nullable=True
    )
    available_balance_inr: Mapped[Decimal | None] = mapped_column(
        Money, nullable=True
    )
    contracts: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    decision: Mapped[SizingDecision] = mapped_column(
        SAEnum(SizingDecision, name="sizing_decision"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Per-bucket capital — Decision 013
# ---------------------------------------------------------------------------
class BucketState(Base, TimestampMixin):
    """Current capital + available balance per bucket.

    capital_inr is the fixed bucket allocation (₹50k by default).
    available_balance_inr and locked_margin_inr MIRROR the bucket's Delta
    sub-account wallet (available / order+position margin, × allocator fx)
    — synced by the reconciler every sweep (Decision 021).
    """

    __tablename__ = "bucket_state"
    __table_args__ = (
        UniqueConstraint("bucket_id", name="uq_bucket_state_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    capital_inr: Mapped[Decimal] = mapped_column(Money, nullable=False)
    available_balance_inr: Mapped[Decimal] = mapped_column(Money, nullable=False)
    locked_margin_inr: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class DailyEquityAnchor(Base, TimestampMixin):
    """Start-of-day (UTC) equity snapshot per execution sub-account.

    The daily-drawdown breaker measures realized + unrealized loss against
    this anchor (Decision 023). The first breaker pass of each UTC day
    writes the row; restarts within the day reuse it, so the anchor
    survives crashes. ``equity`` is in the account's settlement currency
    (USD for Delta India) = wallet balance + unrealized PnL at snapshot.
    """

    __tablename__ = "daily_equity_anchor"
    __table_args__ = (
        UniqueConstraint(
            "account_ref", "date", name="uq_equity_anchor_account_date"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    date: Mapped[date_] = mapped_column(Date, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Money, nullable=False)


class Heartbeat(Base, TimestampMixin):
    """Liveness row per service — the dead-man's switch (Phase 1c).

    The bot-worker upserts its row every tick; the Railway scheduler
    (independent infrastructure from the VM) pages when ``beat_at`` goes
    stale. One row per service name.
    """

    __tablename__ = "heartbeat"
    __table_args__ = (
        UniqueConstraint("service", name="uq_heartbeat_service"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    beat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class DhanToken(Base, TimestampMixin):
    """The one live Dhan access token for a client id, shared across machines.

    Dhan keeps a single active token per client id: minting a new one evicts
    the previous token server-side (established websockets survive, but fresh
    REST calls and new connections on the old token get 401 / DH-906). The
    trading bot (Mumbai VM) and the depth-data recorder (a separate VM) both
    need a live token; if each minted its own they would evict each other
    roughly once a day — the ping-pong documented on 2026-07-23.

    Fix: the routine minter (the bot, on its morning TOTP refresh and any
    self-heal) writes the freshly-minted token to this row, and every other
    process READS it instead of minting a competing one. One row per client
    id. This is the cross-VM analogue of the on-disk token cache the bot's own
    processes already share; see ``src/brokers/dhan/auth.py`` and
    ``token_store.py``. The token is strictly less sensitive than the TOTP
    secret + PIN that mint it.
    """

    __tablename__ = "dhan_token"
    __table_args__ = (
        UniqueConstraint("client_id", name="uq_dhan_token_client"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    minted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Which process last minted — "bot" | "recorder" | other. Forensics only.
    minted_by: Mapped[str] = mapped_column(String(32), nullable=False)


class SessionReport(Base, TimestampMixin):
    """One end-of-day postmortem per trading date (Decision 033, Tier 3).

    Postgres is the store rather than a file because the Railway scheduler
    container is ephemeral and holds no git credentials. The dashboard reads
    this at /journal; ``scripts/export_journal.py`` materialises rows into
    docs/journal/*.md when the user wants them in git.

    One row per date — a re-run overwrites, so the report is always the latest
    view of that session rather than a pile of near-duplicates.
    """

    __tablename__ = "session_report"
    __table_args__ = (
        UniqueConstraint("session_date", name="uq_session_report_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_date: Mapped[date_] = mapped_column(Date, nullable=False)
    # Short Telegram version.
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    # Full journal entry.
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    # The structured numbers behind the prose, for later analysis.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# Re-export for convenience: ``from src.core.models import *`` grabs everything.
__all__ = [
    "BrokerName",
    "OrderSide",
    "PositionSide",
    "OrderStatus",
    "KillSwitchScope",
    "AuditEventType",
    "MarketRegime",
    "SizingDecision",
    "KillSwitch",
    "AuditLog",
    "Trade",
    "Position",
    "StrategyParamChange",
    "DailyUniverse",
    "ScannerSnapshot",
    "SymbolMapping",
    "RegimeModel",
    "RegimeSnapshot",
    "SizingSnapshot",
    "BucketState",
    "DailyEquityAnchor",
    "Heartbeat",
    "DhanToken",
    "SessionReport",
]
