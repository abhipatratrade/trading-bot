"""add symbol column to regime_model + regime_snapshot

Per-coin regime models: previously one HMM per bucket using a single
``proxy_symbol`` (BTC). User wants one HMM per (bucket, symbol) so a
coin can be bull while BTC is bear.

Changes:
- regime_model gains ``symbol VARCHAR(64)``. Old unique constraint
  ``(bucket_id, version)`` drops; new one is ``(bucket_id, symbol, version)``.
- regime_snapshot gains ``symbol VARCHAR(64)``. New composite index on
  ``(bucket_id, symbol, ts)``.
- Existing rows are backfilled with ``symbol='_market_'`` — that's the
  reserved sentinel for the broad-market fallback model that the new
  retrain job will always maintain alongside the per-coin models.

Revision ID: 0005_per_coin_regime
Revises: 0004_uppercase_enums_v2
Create Date: 2026-06-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_per_coin_regime"
down_revision: Union[str, None] = "0004_uppercase_enums_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MARKET_SENTINEL = "_market_"


def upgrade() -> None:
    # ---- regime_model ----------------------------------------------------
    op.add_column(
        "regime_model",
        sa.Column("symbol", sa.String(length=64), nullable=True),
    )
    op.execute(f"UPDATE regime_model SET symbol = '{_MARKET_SENTINEL}'")
    op.alter_column("regime_model", "symbol", nullable=False)

    op.drop_constraint("uq_regime_model_key", "regime_model", type_="unique")
    op.create_unique_constraint(
        "uq_regime_model_key",
        "regime_model",
        ["bucket_id", "symbol", "version"],
    )
    op.create_index(
        "ix_regime_model_bucket_symbol_trained",
        "regime_model",
        ["bucket_id", "symbol", "trained_at"],
    )

    # ---- regime_snapshot -------------------------------------------------
    op.add_column(
        "regime_snapshot",
        sa.Column("symbol", sa.String(length=64), nullable=True),
    )
    op.execute(f"UPDATE regime_snapshot SET symbol = '{_MARKET_SENTINEL}'")
    op.alter_column("regime_snapshot", "symbol", nullable=False)

    op.create_index(
        "ix_regime_snapshot_bucket_symbol_ts",
        "regime_snapshot",
        ["bucket_id", "symbol", "ts"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_regime_snapshot_bucket_symbol_ts", table_name="regime_snapshot"
    )
    op.drop_column("regime_snapshot", "symbol")

    op.drop_index(
        "ix_regime_model_bucket_symbol_trained", table_name="regime_model"
    )
    op.drop_constraint("uq_regime_model_key", "regime_model", type_="unique")
    op.create_unique_constraint(
        "uq_regime_model_key",
        "regime_model",
        ["bucket_id", "version"],
    )
    op.drop_column("regime_model", "symbol")
