"""Every order-placing path must carry the position's PRODUCT, and the stop
sweep must be clocked to the position's OWN venue (2026-09-02).

Two independent bugs left two live MCX NATGASMINI lots with no protection for
the whole of their existence.

**The product.** ``OrderManager.place_order`` defaults ``product`` to None, and
the Dhan adapter then falls back to its constructor default — ``MTF``, a
CASH-EQUITY product. Dhan refuses that on a commodity future with
``DH-906: Trades are not allowed for this Product / Scrip``. The ENTRY path
passed the product; the EXIT path and the BREAKER FLATTEN did not. Five exit
attempts were rejected between 09:03 and 09:10 IST, and the flatten — the one
mechanism that exists for emptying a bucket in an emergency — would have failed
the same way.

**The venue hours.** The sweep asked ``nse_session(now)``, whose default
exchange is NSE (09:15-15:30), for every bucket on the Dhan account. MCX trades
09:00-23:30. Both lots were opened at 23:11 and 23:26 — six hours past NSE
close — so the sweep was gated off and never ATTEMPTED a stop, while the
``stop_coverage`` invariant reported the position uncovered for 344 ticks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.brokers.base import PositionInfo
from src.core.models import PositionSide
from src.safety.enforcement import _flatten_positions
from src.shared.bucket_runner import BucketRunner
from src.shared.market_calendar import NseSession, nse_session


class _CapturingOM:
    """Records place_order kwargs instead of sending anything."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def place_order(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        return None


class _Clock:
    @staticmethod
    def now() -> datetime:
        return datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


# ── the exit path ───────────────────────────────────────────────────────


class _Pos:
    id = 1
    symbol = "NATGASMINI-20260925-FUT"
    side = PositionSide.LONG
    quantity = Decimal("2")


def _runner(product: str | None) -> BucketRunner:
    r = BucketRunner.__new__(BucketRunner)

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.product = product

    class _B:
        id = "commodity-indian"
        config = cfg

    r.bucket = _B()  # type: ignore[attr-defined]
    r._clock = _Clock()  # type: ignore[attr-defined]
    r._collect_mark_prices = lambda syms: {}  # type: ignore[attr-defined]
    return r


class _StubSession:
    """Enough of a Session for the optimistic-close bookkeeping."""

    def get(self, *_a, **_kw):  # noqa: ANN002, ANN003
        return None

    def add(self, *_a, **_kw) -> None:  # noqa: ANN002, ANN003
        return None


def _no_db(monkeypatch) -> None:
    """The exit writes a Position update + audit row after placing. Neither is
    what these tests are about, and the suite runs without a real DB."""
    import contextlib

    import src.shared.bucket_runner as br

    @contextlib.contextmanager
    def _scope():
        yield _StubSession()

    monkeypatch.setattr(br, "session_scope", _scope)


def test_exit_carries_the_positions_product(monkeypatch) -> None:
    """The live failure: without this the order goes out MTF and Dhan
    refuses it, so the position has no working exit at all."""
    _no_db(monkeypatch)
    om = _CapturingOM()
    _runner("MARGIN")._close_position(om, "cci_gas_reversion_15m", _Pos(), None)

    assert len(om.calls) == 1
    assert om.calls[0]["product"] == "MARGIN"
    assert om.calls[0]["reduce_only"] is True
    assert om.calls[0]["side"] == "sell"


def test_exit_product_is_absent_only_when_the_bucket_has_none(monkeypatch) -> None:
    """Crypto has no product dimension; None must still mean 'adapter default'
    rather than an invented value."""
    _no_db(monkeypatch)
    om = _CapturingOM()
    _runner(None)._close_position(om, "s", _Pos(), None)
    assert om.calls[0]["product"] is None


# ── the breaker flatten ─────────────────────────────────────────────────


def test_flatten_carries_the_products_product() -> None:
    """The worst place to omit it: a breaker trips exactly when the bucket
    must be emptied, and a refused order empties nothing."""
    om = _CapturingOM()
    flattened, failed = _flatten_positions(
        positions=[
            PositionInfo(
                symbol="NATGASMINI-20260925-FUT",
                side="long",
                size=Decimal("2"),
                entry_price=Decimal("277.20"),
            )
        ],
        bucket_id="commodity-indian",
        order_manager=om,  # type: ignore[arg-type]
        clock=_Clock(),
        product_by_bucket={"commodity-indian": "MARGIN"},
    )

    assert (flattened, failed) == (1, [])
    assert om.calls[0]["product"] == "MARGIN"
    assert om.calls[0]["reduce_only"] is True
    assert om.calls[0]["allow_when_killed"] is True


def test_flatten_without_a_product_map_still_works() -> None:
    """Crypto passes no map at all; the flatten must not start failing."""
    om = _CapturingOM()
    flattened, _ = _flatten_positions(
        positions=[
            PositionInfo(
                symbol="BTCUSD", side="long",
                size=Decimal("1"), entry_price=Decimal("50000"),
            )
        ],
        bucket_id="longterm-crypto",
        order_manager=om,  # type: ignore[arg-type]
        clock=_Clock(),
    )
    assert flattened == 1
    assert om.calls[0]["product"] is None


# ── the venue the sweep is clocked to ───────────────────────────────────

# 22:00 IST = 16:30 UTC. NSE shut at 15:30 IST; MCX runs to 23:30.
_EVENING = datetime(2026, 9, 2, 16, 30, tzinfo=UTC)
# 09:05 IST = 03:35 UTC. MCX opens 09:00; NSE not until 09:15.
_EARLY = datetime(2026, 9, 2, 3, 35, tzinfo=UTC)


def test_mcx_is_open_when_nse_is_shut() -> None:
    """The contract the sweep gate depends on. Asking about NSE for an MCX
    bucket is what silently withheld the stop."""
    assert nse_session(_EVENING, exchange="NSE") is NseSession.CLOSED
    assert nse_session(_EVENING, exchange="MCX") is not NseSession.CLOSED


def test_mcx_is_open_before_the_equity_open() -> None:
    assert nse_session(_EARLY, exchange="NSE") is NseSession.CLOSED
    assert nse_session(_EARLY, exchange="MCX") is not NseSession.CLOSED


def test_the_default_exchange_is_still_nse() -> None:
    """The fix must not loosen the equity gate: an NSE stop retried at 22:00
    can only be rejected (PIIND, 2026-08-12 — 117 attempts in three hours)."""
    assert nse_session(_EVENING) is NseSession.CLOSED
