"""Broker charges — what Dhan actually billed (2026-08-18).

`Trade.fees` has been a hardcoded zero on every Dhan trade since the
integration was written: `get_fills` reads `/v2/trades`, the intraday day book,
which reports executions and no costs. The costs live on a different resource,
`/v2/trades/{from}/{to}/{page}`.

`realized_pnl` already subtracts `entry.fees + exit.fees` — it has simply been
subtracting nothing. So every P&L figure downstream (dashboard, EOD, tax
ledger, profit factor) has been GROSS of brokerage, STT, stamp duty, exchange
and SEBI charges and GST. For swing-indian, whose backtested mean trade is
~0.62%, that is a large fraction of the edge.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.brokers.base import OrderCharges
from src.brokers.dhan.auth import DhanTokenManager
from src.brokers.dhan.client import DhanClient

_UNIVERSE = {"PIIND": ("1001", "NSE_EQ")}


class _Resp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self) -> object:
        return self._payload


class _FakeHttp:
    def __init__(self, pages: list[object]) -> None:
        self._pages = list(pages)
        self.paths: list[str] = []

    def request(self, method, url, json=None, headers=None):  # noqa: A002
        self.paths.append(url.split(".co", 1)[-1])
        return _Resp(self._pages.pop(0) if self._pages else [])


def _client(http: _FakeHttp) -> DhanClient:
    return DhanClient(
        token_manager=DhanTokenManager(static_token="TOK"),
        client_id="C1",
        resolve_symbol=lambda s: _UNIVERSE[s],
        base_url="https://api.dhan.co",
        http=http,
    )


def _fill(order_id="900", **kw):
    row = {
        "orderId": order_id,
        "brokerageCharges": 20.0,
        "stt": 37.98,
        "exchangeTransactionCharges": 1.13,
        "sebiTax": 0.04,
        "stampDuty": 0.0,
        "serviceTax": 3.81,
    }
    row.update(kw)
    return row


class TestOrderCharges:
    def test_total_sums_every_component(self) -> None:
        c = OrderCharges(
            exchange_order_id="900",
            brokerage=Decimal("20"), stt=Decimal("37.98"),
            exchange_txn=Decimal("1.13"), sebi=Decimal("0.04"),
            stamp_duty=Decimal("5.66"), gst=Decimal("3.81"),
        )
        assert c.total == Decimal("68.62")

    def test_breakdown_survives_jsonb_as_strings(self) -> None:
        d = OrderCharges(exchange_order_id="900", stt=Decimal("37.98")).as_dict()
        assert d["stt"] == "37.98" and d["total"] == "37.98"
        assert all(isinstance(v, str) for v in d.values())


class TestDhanChargesFetch:
    def test_uses_the_dated_trade_history_not_the_day_book(self) -> None:
        """The day book (/v2/trades) carries no charges at all. Reading it is
        why every Dhan fee in this database is zero."""
        http = _FakeHttp([[_fill()], []])
        _client(http).get_order_charges(start=date(2026, 8, 12), end=date(2026, 8, 18))
        assert http.paths[0] == "/v2/trades/2026-08-12/2026-08-18/0"

    def test_maps_every_documented_charge_field(self) -> None:
        http = _FakeHttp([[_fill()], []])
        got = _client(http).get_order_charges(start=date(2026, 8, 18), end=date(2026, 8, 18))
        c = got["900"]
        assert c.brokerage == Decimal("20.0")
        assert c.stt == Decimal("37.98")
        assert c.exchange_txn == Decimal("1.13")
        assert c.sebi == Decimal("0.04")
        assert c.gst == Decimal("3.81")          # Dhan's pre-2017 name
        assert c.total == Decimal("62.96")

    def test_partial_fills_of_one_order_are_summed(self) -> None:
        """Charges arrive per FILL; one Trade row is one ORDER."""
        http = _FakeHttp([[_fill(), _fill()], []])
        got = _client(http).get_order_charges(start=date(2026, 8, 18), end=date(2026, 8, 18))
        assert got["900"].total == Decimal("125.92")

    def test_walks_pages_until_one_comes_back_empty(self) -> None:
        """A truncated read silently UNDER-reports cost, which is worse than
        not reading at all — it looks like a cheaper trade."""
        http = _FakeHttp([[_fill("900")], [_fill("901")], []])
        got = _client(http).get_order_charges(start=date(2026, 8, 18), end=date(2026, 8, 18))
        assert set(got) == {"900", "901"}
        assert len(http.paths) == 3

    def test_a_malformed_field_does_not_lose_the_report(self) -> None:
        http = _FakeHttp([[_fill(stt="n/a")], []])
        got = _client(http).get_order_charges(start=date(2026, 8, 18), end=date(2026, 8, 18))
        assert got["900"].stt == Decimal("0")
        assert got["900"].brokerage == Decimal("20.0")

    def test_other_brokers_report_nothing_and_are_unaffected(self) -> None:
        from src.brokers.delta_india.client import DeltaIndiaClient

        assert DeltaIndiaClient.get_order_charges(
            DeltaIndiaClient, start=date(2026, 8, 18), end=date(2026, 8, 18)
        ) == {}


# ── The two rules that make this safe ────────────────────────────────────
class TestBilledRule:
    """Zero is "not computed yet", not "free"."""

    def test_zero_is_not_a_bill(self) -> None:
        from src.order_manager.reconciler import charges_are_billed

        assert not charges_are_billed(OrderCharges(exchange_order_id="900"))

    def test_missing_report_is_not_a_bill(self) -> None:
        from src.order_manager.reconciler import charges_are_billed

        assert not charges_are_billed(None)

    def test_any_nonzero_component_counts(self) -> None:
        from src.order_manager.reconciler import charges_are_billed

        assert charges_are_billed(
            OrderCharges(exchange_order_id="900", stt=Decimal("37.98"))
        )


class _Trade:
    def __init__(self, tid, extra):
        self.id = tid
        self.symbol = "PIIND"
        self.extra = extra


class _Empty:
    def scalars(self):
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self, by_id):
        self._by_id = by_id

    def get(self, _model, tid):
        return self._by_id.get(tid)

    def execute(self, _stmt):
        return _Empty()


class TestUnstampForRecompute:
    """Charges land AFTER P&L was already finalised with zero fees, so the
    round trip has to be un-finalised and recomputed. Both legs must clear —
    and the entry's `closed_by_trade_id` with them, because the pairing loop
    skips entries already marked closed. Leaving it would strand the exit
    permanently unpairable, with NO P&L at all: worse than the gross figure.
    """

    def _rec(self):
        from src.core.models import BrokerName
        from src.order_manager.reconciler import Reconciler

        return Reconciler(
            broker=None,  # type: ignore[arg-type]
            broker_name=BrokerName.DHAN,
            bucket_ids=["swing-indian"],
        )

    def test_both_legs_are_cleared_including_the_pairing_mark(self) -> None:
        entry = _Trade(1, {"avg_fill_price": "2514.5", "pnl_usd": "195.978",
                           "pnl_final": True, "closed_by_trade_id": 2})
        exit_ = _Trade(2, {"avg_fill_price": "2532.0", "pnl_usd": "195.978",
                           "pnl_final": True, "carry_interest": "66.52",
                           "closed_by_trade_id": 1})
        self._rec()._unstamp_pnl_for_recompute(_FakeSession({1: entry, 2: exit_}), exit_)

        for leg in (entry, exit_):
            for gone in ("pnl_usd", "pnl_final", "pnl_kind", "pnl_pct",
                         "carry_interest", "closed_by_trade_id"):
                assert gone not in leg.extra, f"{gone} survived on {leg.id}"
        # The inputs P&L is recomputed FROM must survive.
        assert entry.extra["avg_fill_price"] == "2514.5"
        assert exit_.extra["avg_fill_price"] == "2532.0"

    def test_a_trade_with_no_pnl_yet_is_untouched(self) -> None:
        t = _Trade(1, {"avg_fill_price": "2514.5"})
        self._rec()._unstamp_pnl_for_recompute(_FakeSession({1: t}), t)
        assert t.extra == {"avg_fill_price": "2514.5"}
