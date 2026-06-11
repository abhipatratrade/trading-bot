"""add UPPERCASE values to sizing_decision and market_regime enums

Same root cause as 0003 (SAEnum serialises Python member NAMES in
uppercase, but migration 0002 created these enums with lowercase
values). Without these UPPERCASE values:

- Every BucketRunner tick that records a SizingSnapshot raises
  psycopg2.errors.InvalidTextRepresentation, the tick fails, and a
  Telegram alert fires ("[bot] tick error in longterm-crypto").
- Brain inference (when enabled) fails to write RegimeSnapshot for the
  same reason.

Discovered 2026-06-12 when the user reported per-minute Telegram alerts
after the prod deploy. Already applied manually to prod; this migration
codifies it so fresh installs of the repo get the correct state.

Revision ID: 0004_uppercase_enums_v2
Revises: 0003_uppercase_enum_values
Create Date: 2026-06-12
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_uppercase_enums_v2"
down_revision: Union[str, None] = "0003_uppercase_enum_values"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SIZING_VALUES = (
    "PLACED",
    "SKIPPED_INSUFFICIENT",
    "SKIPPED_DEDUP",
    "SKIPPED_NEGATIVE_EDGE",
    "SKIPPED_REGIME_GATE",
    "SKIPPED_SCANNER",
    "SKIPPED_OTHER",
)
_REGIME_VALUES = ("BEAR", "NEUTRAL", "BULL")


def upgrade() -> None:
    for v in _SIZING_VALUES:
        op.execute(
            f"ALTER TYPE sizing_decision ADD VALUE IF NOT EXISTS '{v}'"
        )
    for v in _REGIME_VALUES:
        op.execute(f"ALTER TYPE market_regime ADD VALUE IF NOT EXISTS '{v}'")


def downgrade() -> None:
    # Postgres cannot remove individual enum values without rewriting the
    # type. The added values are harmless when unused; leaving them.
    pass
