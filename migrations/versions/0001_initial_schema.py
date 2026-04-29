"""initial schema — Phase 0

Creates every table declared in ``src.core.models`` by delegating to
``Base.metadata.create_all``. We do this rather than hand-writing
``op.create_table(...)`` calls because the model file is the source of
truth and we'd otherwise duplicate it. Future migrations are explicit
``op.alter_column`` / ``op.create_table`` style.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-30
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from src.core.db import Base
from src.core import models  # noqa: F401  — registers all tables on Base.metadata


# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
