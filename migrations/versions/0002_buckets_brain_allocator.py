"""bucket restructure + brain (HMM) + allocator (Kelly) — Phase 1 restructure

Adds:
- new enum types: market_regime, sizing_decision
- new values on broker_name (DHAN) and audit_event_type
- new columns on position and trade: bucket_id, strategy_name
- new tables: regime_model, regime_snapshot, sizing_snapshot, bucket_state
- backfill bucket_id='longterm-crypto' / strategy_name='top5_volume' for
  any existing rows produced by the pre-restructure Phase 1 runner

Revision ID: 0002_buckets_brain_allocator
Revises: 0001_initial_schema
Create Date: 2026-06-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_buckets_brain_allocator"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
market_regime = postgresql.ENUM(
    "bear", "neutral", "bull", name="market_regime", create_type=False
)
sizing_decision = postgresql.ENUM(
    "placed",
    "skipped_insufficient",
    "skipped_dedup",
    "skipped_negative_edge",
    "skipped_regime_gate",
    "skipped_scanner",
    "skipped_other",
    name="sizing_decision",
    create_type=False,
)


def _add_enum_value_if_missing(type_name: str, value: str) -> None:
    """Idempotent ALTER TYPE ... ADD VALUE for Postgres."""
    op.execute(
        f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'"
    )


def upgrade() -> None:
    # ------------------------------------------------------------------ enums
    op.execute("CREATE TYPE market_regime AS ENUM ('bear','neutral','bull')")
    op.execute(
        "CREATE TYPE sizing_decision AS ENUM ("
        "'placed','skipped_insufficient','skipped_dedup',"
        "'skipped_negative_edge','skipped_regime_gate',"
        "'skipped_scanner','skipped_other')"
    )

    # broker_name gains DHAN
    _add_enum_value_if_missing("broker_name", "dhan")

    # audit_event_type gains new entries
    for v in (
        "regime_model_retrained",
        "sizing_decision",
        "strategy_gate_blocked",
    ):
        _add_enum_value_if_missing("audit_event_type", v)

    # ------------------------------------------------------------------ tables
    op.create_table(
        "regime_model",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bucket_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("n_states", sa.Integer(), nullable=False),
        sa.Column(
            "feature_columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "model_blob", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("bucket_id", "version", name="uq_regime_model_key"),
    )
    op.create_index(
        "ix_regime_model_bucket_trained",
        "regime_model",
        ["bucket_id", "trained_at"],
    )

    op.create_table(
        "regime_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("bucket_id", sa.String(length=64), nullable=False),
        sa.Column("regime", market_regime, nullable=False),
        sa.Column(
            "state_probabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("model_version", sa.String(length=64), nullable=False),
    )
    op.create_index(
        "ix_regime_snapshot_bucket_ts", "regime_snapshot", ["bucket_id", "ts"]
    )

    op.create_table(
        "sizing_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("bucket_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("regime", market_regime, nullable=True),
        sa.Column("regime_multiplier", sa.Numeric(8, 4), nullable=True),
        sa.Column("fractional_kelly", sa.Numeric(8, 4), nullable=True),
        sa.Column(
            "kelly_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("suggested_notional_inr", sa.Numeric(28, 12), nullable=True),
        sa.Column("available_balance_inr", sa.Numeric(28, 12), nullable=True),
        sa.Column("contracts", sa.Numeric(28, 12), nullable=True),
        sa.Column("mark_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("decision", sizing_decision, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_sizing_snapshot_bucket_strategy_ts",
        "sizing_snapshot",
        ["bucket_id", "strategy_name", "ts"],
    )
    op.create_index(
        "ix_sizing_snapshot_decision", "sizing_snapshot", ["decision"]
    )

    op.create_table(
        "bucket_state",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bucket_id", sa.String(length=64), nullable=False),
        sa.Column("capital_inr", sa.Numeric(28, 12), nullable=False),
        sa.Column("available_balance_inr", sa.Numeric(28, 12), nullable=False),
        sa.Column(
            "locked_margin_inr",
            sa.Numeric(28, 12),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("bucket_id", name="uq_bucket_state_id"),
    )

    # -------------------------------------------------------- column additions
    op.add_column(
        "position", sa.Column("bucket_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "position", sa.Column("strategy_name", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "ix_position_bucket_symbol", "position", ["bucket_id", "symbol"]
    )

    op.add_column(
        "trade", sa.Column("bucket_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "trade", sa.Column("strategy_name", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "ix_trade_bucket_symbol", "trade", ["bucket_id", "symbol"]
    )

    # -------------------------------------------------------- backfill
    # Legacy crypto_longterm rows: map to (longterm-crypto, top5_volume).
    op.execute(
        "UPDATE position "
        "SET bucket_id='longterm-crypto', strategy_name='top5_volume' "
        "WHERE strategy_id='crypto_longterm'"
    )
    op.execute(
        "UPDATE trade "
        "SET bucket_id='longterm-crypto', strategy_name='top5_volume' "
        "WHERE strategy_id='crypto_longterm'"
    )

    # -------------------------------------------------------- seed bucket_state
    # All six buckets pre-funded with ₹50k each (Decision 013).
    op.execute(
        "INSERT INTO bucket_state "
        "(bucket_id, capital_inr, available_balance_inr, locked_margin_inr, enabled) "
        "VALUES "
        "('longterm-crypto', 50000, 50000, 0, true),"
        "('swing-crypto',    50000, 50000, 0, true),"
        "('scalp-crypto',    50000, 50000, 0, true),"
        "('gambling-crypto', 50000, 50000, 0, true),"
        "('longterm-indian', 50000, 50000, 0, false),"
        "('swing-indian',    50000, 50000, 0, false) "
        "ON CONFLICT (bucket_id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_index("ix_trade_bucket_symbol", table_name="trade")
    op.drop_column("trade", "strategy_name")
    op.drop_column("trade", "bucket_id")

    op.drop_index("ix_position_bucket_symbol", table_name="position")
    op.drop_column("position", "strategy_name")
    op.drop_column("position", "bucket_id")

    op.drop_table("bucket_state")
    op.drop_index("ix_sizing_snapshot_decision", table_name="sizing_snapshot")
    op.drop_index(
        "ix_sizing_snapshot_bucket_strategy_ts", table_name="sizing_snapshot"
    )
    op.drop_table("sizing_snapshot")
    op.drop_index("ix_regime_snapshot_bucket_ts", table_name="regime_snapshot")
    op.drop_table("regime_snapshot")
    op.drop_index("ix_regime_model_bucket_trained", table_name="regime_model")
    op.drop_table("regime_model")

    op.execute("DROP TYPE IF EXISTS sizing_decision")
    op.execute("DROP TYPE IF EXISTS market_regime")
    # NB: we don't drop the added values from broker_name / audit_event_type
    # because Postgres doesn't support removing enum values without recreating
    # the type and any column using it. Leaving them is harmless.
