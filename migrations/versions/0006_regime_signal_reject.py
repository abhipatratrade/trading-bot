"""regime_snapshot.signal column + REGIME_MODEL_REJECTED audit value

Markov 2.0 detection-layer upgrade:
- ``regime_snapshot`` gains a nullable ``signal`` (double precision) column
  holding the continuous regime conviction ``P(bull) − P(bear)`` ∈ [-1, 1].
  Nullable so rows written before this column existed stay valid; downstream
  sizing/gating still keys on ``regime`` (observability only).
- ``audit_event_type`` gains ``REGIME_MODEL_REJECTED`` — emitted by the
  retrain job when a freshly fitted model fails verification and the prior
  model is retained. SAEnum serialises the UPPERCASE member name, matching
  the convention restored in migration 0003.

Revision ID: 0006_regime_signal_reject
Revises: 0005_per_coin_regime
Create Date: 2026-06-30

(Revision id kept <= 32 chars to fit alembic_version.version_num VARCHAR(32).)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_regime_signal_reject"
down_revision: Union[str, None] = "0005_per_coin_regime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "regime_snapshot",
        sa.Column("signal", sa.Float(), nullable=True),
    )
    # ADD VALUE IF NOT EXISTS is idempotent. The new value is not referenced
    # in this migration, so adding it in-transaction is safe (PG 12+).
    op.execute(
        "ALTER TYPE audit_event_type "
        "ADD VALUE IF NOT EXISTS 'REGIME_MODEL_REJECTED'"
    )


def downgrade() -> None:
    op.drop_column("regime_snapshot", "signal")
    # Postgres can't drop an enum value without recreating the type; the
    # added value is harmless when unused, so we leave it (see migration 0003).
