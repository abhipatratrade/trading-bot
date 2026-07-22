"""Reconciler bucket-id scoping (Decision 019).

With one sub-account per bucket, each reconciler must restrict its DB
queries to the bucket(s) on its account so it never sweeps another
bucket's rows. These tests check the WHERE-clause builders directly
(no DB needed); the broker is never touched at construction time.
"""

from __future__ import annotations

from src.core.models import BrokerName
from src.order_manager.reconciler import Reconciler


def _reconciler(bucket_ids: list[str] | None) -> Reconciler:
    # broker is unused at construction and by the scope helpers.
    return Reconciler(
        broker=object(),  # type: ignore[arg-type]
        broker_name=BrokerName.DELTA_INDIA,
        bucket_ids=bucket_ids,
    )


def test_no_bucket_ids_means_no_extra_clauses() -> None:
    rec = _reconciler(None)
    assert rec._scope_positions() == []
    assert rec._scope_trades() == []


def test_bucket_ids_produce_one_clause_each() -> None:
    rec = _reconciler(["longterm-crypto"])
    assert len(rec._scope_positions()) == 1
    assert len(rec._scope_trades()) == 1


def test_clause_targets_bucket_id_column() -> None:
    rec = _reconciler(["longterm-crypto", "swing-crypto"])
    # The clause should compile to an IN over Position.bucket_id.
    clause = rec._scope_positions()[0]
    rendered = str(clause)
    assert "bucket_id" in rendered


# ── Shared-account flag (Decision 027 followup) ─────────────────────────
def test_shared_account_defaults_false() -> None:
    """Crypto (the default) must keep treating the whole account as the bot's."""
    assert _reconciler(None)._shared_account is False


def test_shared_account_flag_stored() -> None:
    rec = Reconciler(
        broker=object(),  # type: ignore[arg-type]
        broker_name=BrokerName.DHAN,
        bucket_ids=["intraday-indian", "swing-indian"],
        shared_account=True,
    )
    assert rec._shared_account is True
