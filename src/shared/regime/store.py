"""
Persist and retrieve fitted HMM artifacts via Postgres JSONB.

``regime_model`` row layout:
    (bucket_id, version) UNIQUE
    model_blob JSONB     — output of RegimeModel.to_dict()
    feature_columns JSONB
    n_states INT
    trained_at TIMESTAMPTZ

The latest version per bucket is the row with the most recent
``trained_at``. ``brain.predict_regime`` calls ``load_latest_for_bucket``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.models import RegimeModel as RegimeModelRow
from src.shared.regime.hmm_model import RegimeModel


def save_model(
    session: Session,
    *,
    bucket_id: str,
    version: str,
    trained_at: datetime,
    model: RegimeModel,
    extra: dict[str, Any] | None = None,
) -> RegimeModelRow:
    """Insert a new fitted-model row. Caller commits."""
    blob = model.to_dict()
    row = RegimeModelRow(
        bucket_id=bucket_id,
        version=version,
        trained_at=trained_at,
        n_states=model.n_states,
        feature_columns={"columns": model.feature_columns},
        model_blob=blob,
        extra=extra,
    )
    session.add(row)
    return row


def load_latest_for_bucket(
    session: Session, bucket_id: str
) -> tuple[str, RegimeModel] | None:
    """Return (version, deserialised model) for the most-recent fit, or None."""
    row = session.execute(
        select(RegimeModelRow)
        .where(RegimeModelRow.bucket_id == bucket_id)
        .order_by(RegimeModelRow.trained_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return row.version, RegimeModel.from_dict(row.model_blob)
