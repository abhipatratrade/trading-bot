"""Ownership is signed when the BUCKET can hold a short — not when a caller
remembers to ask.

Decision 036 added ``signed=`` and ``BucketWatch.derivatives`` to select it.
The flag was never assigned at its one construction site and no caller outside
the invariants passed ``signed``, so the whole path was dead for four months.
commodity-indian opened a genuine NATGASMINI short on 2026-09-04 and every
account-level check filed it under the user's own trading::

    reconciler   refused to adopt it -> no Position row -> select_exits never ran
    stop sweep   nothing to protect
    invariants   "foreign_positions: positions the bot does NOT own"

The default now derives from the bucket, so a future caller cannot reintroduce
the bug by forgetting a keyword. A cash bucket must keep the pre-036 long-only
view exactly — that is what stops a settlement artifact (Dhan reports a sale
out of holdings as a negative day-position) becoming a "short" the bot manages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from src.core.models import BrokerName, OrderSide, OrderStatus
from src.order_manager.ownership import bot_owned_quantities, bucket_allows_shorts

_NOW = datetime(2026, 9, 4, 18, 30, tzinfo=UTC)
_SYM = "NATGASMINI-20260925-FUT"


@dataclass
class _T:
    symbol: str
    side: OrderSide
    quantity: Decimal
    status: OrderStatus = OrderStatus.FILLED
    extra: dict = field(default_factory=dict)


class _Session:
    """Just enough of a Session for the query wrapper."""

    def __init__(self, rows: list[_T]) -> None:
        self._rows = rows

    def execute(self, _stmt):  # noqa: ANN001
        rows = self._rows

        class _R:
            def scalars(self):
                class _S:
                    def all(self):
                        return rows

                return _S()

        return _R()


def _short_one() -> list[_T]:
    """The live shape: bot bought a lot, exited it, then SOLD TO OPEN."""
    return [
        _T(_SYM, OrderSide.BUY, Decimal("1")),
        _T(_SYM, OrderSide.SELL, Decimal("1"), extra={"reduce_only": True}),
        _T(_SYM, OrderSide.SELL, Decimal("1"), extra={"reduce_only": False}),
    ]


def _owned(bucket: str, rows: list[_T], **kw) -> dict[str, Decimal]:
    return bot_owned_quantities(
        _Session(rows),  # type: ignore[arg-type]
        broker_name=BrokerName.DHAN,
        bucket_ids=[bucket],
        now=_NOW,
        **kw,
    )


# ── which buckets may hold a short ──────────────────────────────────────


def test_a_derivative_bucket_allows_shorts() -> None:
    """Read off the real buckets.yaml — selling to open is an ordinary entry
    for the CCI gas strategy (64 of its 125 backtested trades are sells)."""
    assert bucket_allows_shorts("commodity-indian") is True


def test_a_cash_equity_bucket_does_not() -> None:
    """A demat account carries no short; a short row there is corrupt."""
    assert bucket_allows_shorts("swing-indian") is False
    assert bucket_allows_shorts("intraday-indian") is False


def test_an_unknown_bucket_is_refused_not_raised() -> None:
    """This sits inside the check every safety sweep depends on."""
    assert bucket_allows_shorts("no-such-bucket") is False


# ── the derived default ─────────────────────────────────────────────────


def test_the_bots_own_short_is_visible_on_a_derivative_bucket() -> None:
    """The live failure. Absent from this dict means no stop, no flatten, no
    reconciliation — the position exists and nothing believes it owns it."""
    owned = _owned("commodity-indian", _short_one())
    assert owned.get(_SYM) == Decimal("-1")


def test_a_cash_bucket_keeps_the_long_only_view() -> None:
    """Unchanged from pre-036: a net-negative cash symbol is not the bot's,
    because that is what a sale out of holdings looks like before settlement."""
    assert _owned("swing-indian", _short_one()) == {}


def test_a_long_reads_the_same_either_way() -> None:
    """The derivation must only ever ADD shorts, never disturb longs."""
    rows = [_T(_SYM, OrderSide.BUY, Decimal("2"))]
    assert _owned("commodity-indian", rows) == {_SYM: Decimal("2")}
    assert _owned("swing-indian", rows) == {_SYM: Decimal("2")}


def test_a_flat_symbol_is_absent_from_both_views() -> None:
    rows = [
        _T(_SYM, OrderSide.BUY, Decimal("1")),
        _T(_SYM, OrderSide.SELL, Decimal("1"), extra={"reduce_only": True}),
    ]
    assert _owned("commodity-indian", rows) == {}
    assert _owned("swing-indian", rows) == {}


# ── the override still works ────────────────────────────────────────────


def test_an_explicit_signed_flag_beats_the_bucket() -> None:
    """session_invariants passes it explicitly; that must keep working."""
    assert _owned("commodity-indian", _short_one(), signed=False) == {}
    assert _owned("swing-indian", _short_one(), signed=True) == {
        _SYM: Decimal("-1")
    }


def test_a_mixed_account_is_signed_if_any_bucket_can_be() -> None:
    """One reconciler and one sweep serve every Dhan bucket at once. Deciding
    long-only because most of them are cash would hide the derivative one's
    short — the account-level view has to admit the widest case."""
    owned = bot_owned_quantities(
        _Session(_short_one()),  # type: ignore[arg-type]
        broker_name=BrokerName.DHAN,
        bucket_ids=["swing-indian", "intraday-indian", "commodity-indian"],
        now=_NOW,
    )
    assert owned.get(_SYM) == Decimal("-1")


def test_no_buckets_means_no_holdings() -> None:
    assert (
        bot_owned_quantities(
            _Session(_short_one()),  # type: ignore[arg-type]
            broker_name=BrokerName.DHAN,
            bucket_ids=[],
            now=_NOW,
        )
        == {}
    )
