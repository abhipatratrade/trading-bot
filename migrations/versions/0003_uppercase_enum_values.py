"""align enum values with SAEnum's UPPERCASE name convention

Background:
- Migration 0001 created the audit_event_type and broker_name enums via
  ``Base.metadata.create_all``. SQLAlchemy's ``SAEnum`` defaults to using
  the Python member NAMES (uppercase: ``BOT_STARTUP``, ``DELTA_INDIA``)
  as the database values, not the ``.value`` strings.
- Migration 0002 added new enum values for the bucket restructure
  (``sizing_decision``, ``strategy_gate_blocked``, ``regime_model_retrained``,
  ``dhan``) in LOWERCASE, breaking the convention. SAEnum still serialised
  member names in UPPERCASE so any INSERT using the new types raised
  ``psycopg2.errors.InvalidTextRepresentation``.

Fix:
- This migration adds the UPPERCASE versions of those enum values so the
  bot can actually write them. The lowercase values added by 0002 remain
  but are unused by the current code; harmless to leave.

Revision ID: 0003_uppercase_enum_values
Revises: 0002_buckets_brain_allocator
Create Date: 2026-06-12
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_uppercase_enum_values"
down_revision: Union[str, None] = "0002_buckets_brain_allocator"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AUDIT_VALUES = (
    "REGIME_MODEL_RETRAINED",
    "SIZING_DECISION",
    "STRATEGY_GATE_BLOCKED",
)
_BROKER_VALUES = ("DHAN",)


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE IF NOT EXISTS is idempotent. The new values
    # cannot be referenced in the SAME transaction they're added in, but
    # that's fine here — no subsequent statement in this migration uses
    # them.
    for v in _AUDIT_VALUES:
        op.execute(
            f"ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS '{v}'"
        )
    for v in _BROKER_VALUES:
        op.execute(f"ALTER TYPE broker_name ADD VALUE IF NOT EXISTS '{v}'")


def downgrade() -> None:
    # Postgres does not support removing values from an enum without
    # recreating the type, which would force a re-write of every column
    # using it. We deliberately leave the added values in place on
    # downgrade — they are harmless when unused.
    pass
