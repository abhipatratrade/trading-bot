"""Which un-tracked exchange positions the reconciler may adopt.

The reconciler refused EVERY short on a shared account. That rule was written
for cash equity, where a short row is corrupt (Dhan reports a sale out of
holdings as a negative day-position until settlement — PIIND, 2026-08-18).

commodity-indian trades futures, where selling to open is an ordinary entry.
On 2026-09-04 the bot sold 1 NATGASMINI to open, this branch refused it, no
Position row was written, and ``bucket_runner`` short-circuits on an empty
ledger (``if not held_rows: return 0``) — so ``select_exits`` never ran and the
strategy could not close its own position. Only the attached stop, 12.50 away,
was still live.

No DB here: the decision is a pure predicate over (side, latest trade), which
is the same shape as the scoping helpers next door.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.models import BrokerName
from src.order_manager.reconciler import Reconciler


@dataclass
class _Trade:
    bucket_id: str | None


def _rec(*, shared: bool) -> Reconciler:
    # The broker is untouched at construction and by this predicate.
    return Reconciler(
        broker=object(),  # type: ignore[arg-type]
        broker_name=BrokerName.DHAN,
        bucket_ids=["swing-indian", "commodity-indian"],
        shared_account=shared,
    )


# ── the live regression ─────────────────────────────────────────────────


def test_a_derivative_bucket_short_is_adopted() -> None:
    """The 2026-09-04 NATGASMINI short. Refusing it left the position with no
    exit engine at all."""
    assert _rec(shared=True)._may_adopt_orphan("short", _Trade("commodity-indian"))


def test_a_cash_bucket_short_is_still_refused() -> None:
    """PIIND. Adopting a settlement artifact is how it became state."""
    assert not _rec(shared=True)._may_adopt_orphan("short", _Trade("swing-indian"))


# ── the unattributable cases ────────────────────────────────────────────


def test_a_short_with_no_trade_is_refused() -> None:
    """No trade means no bucket to ask, and 'we cannot say whose this is' is
    the artifact case rather than the derivative one."""
    assert not _rec(shared=True)._may_adopt_orphan("short", None)


def test_a_short_whose_trade_has_no_bucket_is_refused() -> None:
    """Pre-Decision-013 rows carry no bucket_id."""
    assert not _rec(shared=True)._may_adopt_orphan("short", _Trade(None))


def test_an_unknown_bucket_short_is_refused() -> None:
    assert not _rec(shared=True)._may_adopt_orphan("short", _Trade("no-such-bucket"))


# ── everything the guard must not touch ─────────────────────────────────


def test_longs_are_never_gated() -> None:
    """This branch is about shorts only; a long orphan has always imported."""
    rec = _rec(shared=True)
    assert rec._may_adopt_orphan("long", _Trade("swing-indian"))
    assert rec._may_adopt_orphan("long", None)


def test_an_exclusive_account_adopts_everything() -> None:
    """Crypto sub-accounts are the bot's alone (Decision 019) — there is no
    user's-position question to ask, so no bucket to consult either."""
    rec = _rec(shared=False)
    assert rec._may_adopt_orphan("short", _Trade("swing-indian"))
    assert rec._may_adopt_orphan("short", None)
