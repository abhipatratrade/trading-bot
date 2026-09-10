"""The two halves of a reconcile pass must agree about shorts.

``_reconcile_positions`` runs Case 0 (flatten a corrupt SHORT row) and then
Case 2 (adopt an un-tracked exchange position) in the SAME transaction. They
ask the same question -- "can this bucket legitimately hold a short?" -- and
for four days only one of them knew the answer.

``cab3660`` wrote Case 0 for cash equity, where a short row IS corrupt: Dhan
reports a sale out of holdings as a negative day-position until settlement
(PIIND, 2026-08-18). Decision 037 then taught Case 2 to ask the bucket, because
on MCX futures selling to open is an ordinary entry. Case 0 was not migrated.

So Case 0 flattened the NATGASMINI short and Case 2 re-adopted it, every pass,
~7 minutes apart, 632 times between 2026-09-04 19:17 and 2026-09-08 03:44 --
stopping only when the strategy finally exited. Each cycle reset ``opened_at``,
re-read ``entry_price`` off the exchange, and wrote a junk RECONCILE_DIFF.

Both halves are pure predicates, so this needs no DB -- which is also why the
bug was invisible: each half was tested alone, and each was correct alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.models import BrokerName, PositionSide
from src.order_manager.reconciler import Reconciler

# swing-indian is cash equity (long-only); commodity-indian trades MCX futures.
_CASH = "swing-indian"
_DERIVATIVE = "commodity-indian"
_BUCKETS = [None, _CASH, _DERIVATIVE, "no-such-bucket"]


@dataclass
class _Trade:
    bucket_id: str | None


def _rec(*, shared: bool = True) -> Reconciler:
    # The broker is untouched at construction and by either predicate.
    return Reconciler(
        broker=object(),  # type: ignore[arg-type]
        broker_name=BrokerName.DHAN,
        bucket_ids=[_CASH, _DERIVATIVE],
        shared_account=shared,
    )


# ── the invariant the live bug broke ────────────────────────────────────


def test_short_row_flatten_agrees_with_adoption() -> None:
    """The property that would have caught this on 2026-09-04.

    Destroying a row the very next step re-creates is an oscillation, and
    keeping a row the next step would refuse to adopt is a phantom. For every
    bucket, exactly one of the two halves must claim the short.
    """
    for shared in (True, False):
        rec = _rec(shared=shared)
        for bucket_id in _BUCKETS:
            flattens = rec._must_flatten_short_row(PositionSide.SHORT, bucket_id)
            adopts = rec._may_adopt_orphan("short", _Trade(bucket_id))
            assert flattens is not adopts, (
                f"shared={shared} bucket={bucket_id}: flatten={flattens} "
                f"adopt={adopts} -- the halves disagree, so the row will "
                f"oscillate for as long as the position is open"
            )


# ── the live regression ─────────────────────────────────────────────────


def test_a_derivative_bucket_short_row_survives() -> None:
    """NATGASMINI. Selling to open is an ordinary entry on MCX."""
    assert not _rec()._must_flatten_short_row(PositionSide.SHORT, _DERIVATIVE)


def test_a_cash_bucket_short_row_is_still_flattened() -> None:
    """PIIND, and the whole reason Case 0 exists. A short row here feeds
    ``_run_exits`` a phantom, and exits bypass the kill switch (Decision 024)."""
    assert _rec()._must_flatten_short_row(PositionSide.SHORT, _CASH)


# ── the unattributable cases ────────────────────────────────────────────


def test_a_short_row_with_no_bucket_is_flattened() -> None:
    """No bucket to ask, so it keeps the cash reading: "we cannot say whose
    this is" is the artifact case, not the derivative one."""
    assert _rec()._must_flatten_short_row(PositionSide.SHORT, None)


def test_an_unknown_bucket_short_row_is_flattened() -> None:
    """``bucket_allows_shorts`` answers False for an id not in buckets.yaml."""
    assert _rec()._must_flatten_short_row(PositionSide.SHORT, "no-such-bucket")


# ── everything the guard must not touch ─────────────────────────────────


def test_longs_are_never_flattened() -> None:
    rec = _rec()
    for bucket_id in _BUCKETS:
        assert not rec._must_flatten_short_row(PositionSide.LONG, bucket_id)
        assert not rec._must_flatten_short_row(PositionSide.FLAT, bucket_id)


def test_an_exclusive_account_flattens_nothing() -> None:
    """Delta sub-accounts are the bot's alone (Decision 019) -- there is no
    user's-position question to ask, so no bucket to consult either."""
    rec = _rec(shared=False)
    for bucket_id in _BUCKETS:
        assert not rec._must_flatten_short_row(PositionSide.SHORT, bucket_id)
