"""A futures selection must not require a spot price (Decision 037).

commodity-indian went live 2026-08-31 and was structurally incapable of
trading. Every tick logged ``contract_selection_no_spot`` for NATGASMINI,
dropped its only candidate, and returned an empty plan — so the scanner was
never reached and no order could ever be placed. The bucket looked healthy the
whole time: ``bucket_run_start``, ``bucket_run_complete``, ``blocked=[]``.

The cause was a gate in ``_resolve_contracts`` that demanded a positive spot
price for EVERY candidate. Spot exists to place a STRIKE, and a future has no
strike — ``ContractSelector.select`` says so in its own docstring ("Only
consulted for options; a future needs no strike"). Worse, NATGASMINI has no
spot series to fetch at all: MCX quotes it only as futures, which is exactly
why that bucket's contracts.yaml carries ``signal_source: contract``.

Nothing covered ``_resolve_contracts``, which is how a runner could contradict
its own selector for a full session without a test going red.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.data_sources.dhan_fno import DerivativeContract
from src.shared.bucket_runner import BucketRunner
from src.shared.contract_selection import ContractSelectionConfig

_FRONT = DerivativeContract(
    symbol="NATGASMINI-20260925-FUT",
    security_id="568246",
    exchange_segment="MCX_COMM",
    underlying="NATGASMINI",
    instrument="FUTCOM",
    expiry=date(2026, 9, 25),
    lot_size=250,
    tick_size=Decimal("0.10"),
    freeze_qty=0,
    multiplier=Decimal("250"),
)


class _StubRegistry:
    def futures(self, underlying: str) -> list[DerivativeContract]:
        return [_FRONT] if underlying == "NATGASMINI" else []

    def options(self, underlying: str) -> list[DerivativeContract]:  # noqa: ARG002
        return []


class _StubData:
    fno = _StubRegistry()

    def get_ticker(self, symbol: str):  # noqa: ARG002
        class _T:
            last_price = Decimal("276.10")

        return _T()


class _StubClock:
    @staticmethod
    def now():
        from datetime import datetime

        return datetime(2026, 8, 31, 20, 0, 0)


def _runner() -> BucketRunner:
    """A runner with only the parts _resolve_contracts touches."""
    r = BucketRunner.__new__(BucketRunner)
    r._data = _StubData()  # type: ignore[attr-defined]
    r._clock = _StubClock()  # type: ignore[attr-defined]
    r.contract_configs = {  # type: ignore[attr-defined]
        "": ContractSelectionConfig(
            instrument="future",
            expiry_rule="nearest",
            min_days_to_expiry=15,
            signal_source="contract",
        )
    }

    class _B:
        id = "commodity-indian"

    r.bucket = _B()  # type: ignore[attr-defined]
    return r


def test_future_resolves_with_no_spot_price_at_all() -> None:
    """The live failure, pinned: no spot for NATGASMINI anywhere."""
    plan = _runner()._resolve_contracts(
        scanner="",
        symbols=["NATGASMINI"],
        sides={"NATGASMINI": "buy"},
        spot_prices={},  # <- MCX quotes no spot series for this underlying
    )

    assert plan.symbols == ["NATGASMINI"]
    assert plan.exec_symbols["NATGASMINI"] == "NATGASMINI-20260925-FUT"
    # Priced off the CONTRACT, which is where a future's price lives.
    assert plan.exec_prices["NATGASMINI"] == Decimal("276.10")
    assert plan.lot_sizes["NATGASMINI"] == Decimal("250")


def test_future_resolves_with_a_zero_spot() -> None:
    """A feed that answers 0 must be as harmless as one that answers nothing."""
    plan = _runner()._resolve_contracts(
        scanner="",
        symbols=["NATGASMINI"],
        sides={"NATGASMINI": "buy"},
        spot_prices={"NATGASMINI": Decimal("0")},
    )
    assert plan.symbols == ["NATGASMINI"]


def test_option_selection_still_demands_a_spot() -> None:
    """The gate is right for options — a strike cannot be placed without one.

    Removing it wholesale would have swapped an inert bucket for one picking
    strikes off a dummy price, which is far worse than trading nothing.
    """
    r = _runner()
    r.contract_configs[""] = ContractSelectionConfig(  # type: ignore[attr-defined]
        instrument="option",
        strike_rule="atm",
        expiry_rule="nearest",
        min_days_to_expiry=2,
    )

    plan = r._resolve_contracts(
        scanner="",
        symbols=["NATGASMINI"],
        sides={"NATGASMINI": "buy"},
        spot_prices={},
    )
    assert plan.symbols == []
