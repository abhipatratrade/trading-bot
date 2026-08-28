"""Dhan F&O contract registry — symbol minting, parsing, lookup (Decision 036).

The load-bearing test in this file is
``test_weekly_expiries_get_distinct_symbols``. Dhan's own ``SYMBOL_NAME``
carries only the expiry MONTH, so five different NIFTY weeklies share the
string ``NIFTY-Sep2026-23150-CE``. If the registry ever regresses to that
field, a strategy with a days-to-expiry rule silently trades the wrong
contract and nothing else in the system notices.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.data_sources.dhan_fno import (
    NSE_FNO,
    DerivativeContract,
    FnoRegistry,
    _keep_nearest_expiries,
    contract_symbol,
)

# Far enough out that these never expire out from under the suite.
_EXP_1 = date.today() + timedelta(days=7)
_EXP_2 = date.today() + timedelta(days=14)
_EXP_3 = date.today() + timedelta(days=40)


def _contract(
    underlying: str = "NIFTY",
    *,
    expiry: date = _EXP_1,
    strike: Decimal | None = Decimal("23150"),
    option_type: str | None = "CE",
    instrument: str = "OPTIDX",
    security_id: str = "1",
    lot_size: int = 65,
    tick_size: Decimal = Decimal("0.05"),
    freeze_qty: int = 1756,
) -> DerivativeContract:
    return DerivativeContract(
        symbol=contract_symbol(
            underlying, expiry, strike=strike, option_type=option_type
        ),
        security_id=security_id,
        exchange_segment=NSE_FNO,
        underlying=underlying,
        instrument=instrument,
        expiry=expiry,
        lot_size=lot_size,
        tick_size=tick_size,
        freeze_qty=freeze_qty,
        strike=strike,
        option_type=option_type,
    )


# ── symbol minting ──────────────────────────────────────────────────────
def test_option_symbol_carries_full_expiry_date() -> None:
    sym = contract_symbol(
        "NIFTY", date(2026, 9, 8), strike=Decimal("23150"), option_type="CE"
    )
    assert sym == "NIFTY-20260908-23150-CE"


def test_future_symbol_has_no_strike() -> None:
    assert contract_symbol("NIFTY", date(2026, 9, 29)) == "NIFTY-20260929-FUT"


def test_whole_strike_never_renders_in_exponent_form() -> None:
    """``Decimal("23150.00000").normalize()`` is ``2.315E+4`` — unusable as a key."""
    sym = contract_symbol(
        "NIFTY", date(2026, 9, 8), strike=Decimal("23150.00000"), option_type="PE"
    )
    assert sym == "NIFTY-20260908-23150-PE"
    assert "E+" not in sym


def test_fractional_strike_is_preserved() -> None:
    """2,319 NSE strikes are half-points; an int cast would collide 42.5 with 42."""
    sym = contract_symbol(
        "IDEA", date(2026, 9, 29), strike=Decimal("42.50000"), option_type="CE"
    )
    assert sym == "IDEA-20260929-42.5-CE"


def test_weekly_expiries_get_distinct_symbols() -> None:
    """The reason this registry mints its own symbol at all.

    All five of these are ``NIFTY-Sep2026-23150-CE`` in Dhan's SYMBOL_NAME.
    """
    weeklies = [date(2026, 9, d) for d in (1, 8, 15, 22, 29)]
    symbols = {
        contract_symbol("NIFTY", e, strike=Decimal("23150"), option_type="CE")
        for e in weeklies
    }
    assert len(symbols) == 5


def test_minted_symbol_fits_the_symbol_column() -> None:
    """``Position.symbol`` / ``Trade.symbol`` are String(64)."""
    longest = contract_symbol(
        "PERSISTENT",  # longest NSE F&O underlying, 10 chars
        date(2026, 12, 29),
        strike=Decimal("123456.5"),
        option_type="CE",
    )
    assert len(longest) <= 64


# ── contract properties ─────────────────────────────────────────────────
def test_stock_derivatives_are_physically_settled() -> None:
    """The Phase D square-off rule keys on this; index must NOT be flagged."""
    assert _contract(instrument="OPTSTK").physically_settled
    assert _contract(instrument="FUTSTK").physically_settled
    assert not _contract(instrument="OPTIDX").physically_settled
    assert not _contract(instrument="FUTIDX").physically_settled


def test_spec_exposes_lot_tick_and_freeze() -> None:
    spec = _contract(lot_size=65, tick_size=Decimal("0.50"), freeze_qty=1756).spec()
    assert spec.lot_size == Decimal("65")
    assert spec.tick_size == Decimal("0.50")
    assert spec.freeze_qty == Decimal("1756")


def test_zero_freeze_qty_means_uncapped_not_zero() -> None:
    """A 0 in the master means "no published cap"; passing it through as a
    quantity ceiling would refuse every order."""
    assert _contract(freeze_qty=0).spec().freeze_qty is None


def test_days_to_expiry() -> None:
    c = _contract(expiry=date(2026, 9, 10))
    assert c.days_to_expiry(date(2026, 9, 1)) == 9


def test_dict_round_trip_is_lossless() -> None:
    """The on-disk cache goes through this; a lost Decimal would mis-size."""
    c = _contract(strike=Decimal("42.5"), tick_size=Decimal("0.01"))
    assert DerivativeContract.from_dict(c.to_dict()) == c


# ── registry lookup ─────────────────────────────────────────────────────
@pytest.fixture
def registry() -> FnoRegistry:
    return FnoRegistry(
        [
            _contract(security_id="101", expiry=_EXP_1, strike=Decimal("23000")),
            _contract(security_id="102", expiry=_EXP_1, strike=Decimal("23150")),
            _contract(
                security_id="103", expiry=_EXP_1, strike=Decimal("23150"),
                option_type="PE",
            ),
            _contract(security_id="104", expiry=_EXP_2, strike=Decimal("23150")),
            _contract(
                security_id="105", expiry=_EXP_1, strike=None, option_type=None,
                instrument="FUTIDX",
            ),
            _contract(
                "RELIANCE", security_id="201", expiry=_EXP_3,
                strike=Decimal("1400"), instrument="OPTSTK", lot_size=500,
            ),
        ]
    )


def test_resolve_returns_security_id_and_segment(registry: FnoRegistry) -> None:
    sid, seg = registry.resolve(f"NIFTY-{_EXP_1:%Y%m%d}-23150-CE")
    assert (sid, seg) == ("102", NSE_FNO)


def test_resolve_raises_on_unknown_contract(registry: FnoRegistry) -> None:
    """Fail closed: an unknown contract must never fall through to an order."""
    with pytest.raises(ValueError, match="Unknown Dhan F&O contract"):
        registry.resolve("NIFTY-20260101-1-CE")


def test_by_security_id_is_the_format_proof_join(registry: FnoRegistry) -> None:
    """Dhan's F&O ``tradingSymbol`` format is unverified against a live
    account; ``securityId`` is on every payload and is unique."""
    c = registry.by_security_id("104")
    assert c is not None and c.expiry == _EXP_2


def test_expiries_are_sorted_ascending(registry: FnoRegistry) -> None:
    assert registry.expiries("NIFTY") == [_EXP_1, _EXP_2]


def test_chain_filters_by_expiry_and_type(registry: FnoRegistry) -> None:
    calls = registry.chain("NIFTY", _EXP_1, option_type="CE")
    assert [c.strike for c in calls] == [Decimal("23000"), Decimal("23150")]
    # The future shares the expiry but is not part of an option chain.
    assert all(c.is_option for c in calls)


def test_futures_excludes_options(registry: FnoRegistry) -> None:
    futs = registry.futures("NIFTY")
    assert len(futs) == 1
    assert futs[0].security_id == "105"
    assert futs[0].strike is None and futs[0].option_type is None


def test_underlyings(registry: FnoRegistry) -> None:
    assert registry.underlyings() == {"NIFTY", "RELIANCE"}


def test_spec_lookup_returns_none_for_unknown(registry: FnoRegistry) -> None:
    assert registry.spec("NOPE-20260101-1-CE") is None


# ── expiry trimming ─────────────────────────────────────────────────────
def test_keep_nearest_expiries_is_per_underlying() -> None:
    """NIFTY lists 18 expiries where a stock lists 3 — the trim must not let
    the index's ladder starve the stock's."""
    contracts = [
        _contract(security_id="1", expiry=_EXP_1),
        _contract(security_id="2", expiry=_EXP_2),
        _contract(security_id="3", expiry=_EXP_3),
        _contract("RELIANCE", security_id="4", expiry=_EXP_3, instrument="OPTSTK"),
    ]
    kept = _keep_nearest_expiries(contracts, 2)
    assert {c.security_id for c in kept} == {"1", "2", "4"}


# ── scrip-master parsing ────────────────────────────────────────────────
_HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,"
    "LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,SM_FREEZE_QTY"
)


class _CsvResp:
    def __init__(self, text: str) -> None:
        self.content = text.encode()

    def raise_for_status(self) -> None:
        return None


class _CsvHttp:
    def __init__(self, text: str) -> None:
        self._text = text
        self.gets = 0

    def get(self, url: str, timeout: float | None = None) -> _CsvResp:  # noqa: ARG002
        self.gets += 1
        return _CsvResp(self._text)


def _parse(rows: list[str], tmp_path, **kwargs) -> list[DerivativeContract]:
    """Run the real chunked parse over a synthetic master."""
    http = _CsvHttp("\n".join([_HEADER, *rows]))
    reg = FnoRegistry(
        http=http, cache_path=tmp_path / "fno.json", **kwargs
    )
    return reg.contracts


def _row(
    *,
    security_id: str = "1",
    instrument: str = "OPTIDX",
    underlying: str = "NIFTY",
    lot: str = "65",
    expiry: date | str = _EXP_1,
    strike: str = "23150.00000",
    opt: str = "CE",
    tick: str = "5.0000",
    freeze: str = "1756",
    exch: str = "NSE",
    segment: str = "D",
) -> str:
    exp = expiry if isinstance(expiry, str) else expiry.isoformat()
    return (
        f"{exch},{segment},{security_id},{instrument},{underlying},"
        f"{lot},{exp},{strike},{opt},{tick},{freeze}"
    )


def test_parse_converts_tick_from_paise(tmp_path) -> None:
    """TICK_SIZE is in paise — verified against NSE cash equity, whose known
    Rs 0.05 tick reads as 5.0000. A stock future at 50.0000 is Rs 0.50."""
    got = _parse(
        [
            _row(security_id="1", tick="5.0000"),
            _row(
                security_id="2", tick="50.0000", instrument="FUTSTK",
                underlying="RELIANCE", strike="-0.01000", opt="XX",
            ),
        ],
        tmp_path,
    )
    ticks = sorted(c.tick_size for c in got)
    assert ticks == [Decimal("0.05"), Decimal("0.5")]


def test_symbol_collision_is_logged_not_silent(tmp_path, capsys) -> None:
    """Two rows minting one symbol must not quietly shadow each other — that
    is the exact failure mode ``SYMBOL_NAME`` has and this registry avoids.

    ``capsys``, not ``caplog``: structlog writes to stdout rather than through
    the stdlib logging tree that caplog hooks.
    """
    got = _parse(
        [_row(security_id="1"), _row(security_id="2")],  # identical key tuple
        tmp_path,
    )
    assert len(got) == 1  # a dict cannot hold both
    assert "fno_symbol_collision" in capsys.readouterr().out


def test_parse_normalises_futures_sentinels(tmp_path) -> None:
    """Futures carry OPTION_TYPE='XX' and STRIKE_PRICE=-0.01, not nulls."""
    got = _parse(
        [_row(instrument="FUTIDX", strike="-0.01000", opt="XX", security_id="9")],
        tmp_path,
    )
    assert len(got) == 1
    fut = got[0]
    assert fut.strike is None
    assert fut.option_type is None
    assert fut.is_future
    assert fut.symbol.endswith("-FUT")


def test_parse_drops_expired_contracts(tmp_path) -> None:
    """A dead contract still resolves, so a caller could place an order the
    venue refuses. Never load it."""
    got = _parse(
        [
            _row(security_id="live", expiry=_EXP_1),
            _row(security_id="dead", expiry=date.today() - timedelta(days=1)),
        ],
        tmp_path,
    )
    assert {c.security_id for c in got} == {"live"}


def test_parse_keeps_only_the_derivative_segment(tmp_path) -> None:
    got = _parse(
        [
            _row(security_id="fno", segment="D"),
            _row(security_id="eq", segment="E"),
            _row(security_id="cur", segment="C"),
        ],
        tmp_path,
    )
    assert {c.security_id for c in got} == {"fno"}


def test_parse_keeps_only_the_requested_exchange(tmp_path) -> None:
    """BSE derivatives are explicitly out of scope for v1."""
    got = _parse(
        [_row(security_id="nse", exch="NSE"), _row(security_id="bse", exch="BSE")],
        tmp_path,
    )
    assert {c.security_id for c in got} == {"nse"}


def test_parse_scopes_to_requested_underlyings(tmp_path) -> None:
    """The production path — the whole NSE segment is 74k contracts."""
    got = _parse(
        [
            _row(security_id="n", underlying="NIFTY"),
            _row(security_id="r", underlying="RELIANCE", instrument="OPTSTK"),
        ],
        tmp_path,
        underlyings={"NIFTY"},
    )
    assert {c.security_id for c in got} == {"n"}


def test_parse_skips_malformed_rows_without_failing_the_batch(tmp_path) -> None:
    """One bad row in a 74k-row public CSV must not take the catalogue down."""
    got = _parse(
        [
            _row(security_id="good"),
            _row(security_id="bad", lot="not-a-number"),
            _row(security_id="alsobad", expiry="not-a-date"),
            _row(security_id="good2", strike="23200.00000"),
        ],
        tmp_path,
    )
    assert {c.security_id for c in got} == {"good", "good2"}


def test_parse_rejects_zero_lot_size(tmp_path) -> None:
    """A zero lot would make the sizer divide by zero or size infinitely."""
    assert _parse([_row(lot="0")], tmp_path) == []


def test_second_read_uses_the_cache(tmp_path) -> None:
    http = _CsvHttp("\n".join([_HEADER, _row()]))
    cache = tmp_path / "fno.json"
    first = FnoRegistry(http=http, cache_path=cache)
    assert len(first.contracts) == 1
    second = FnoRegistry(http=http, cache_path=cache)
    assert len(second.contracts) == 1
    assert http.gets == 1, "second registry re-downloaded instead of reading cache"


def test_scoped_registries_do_not_share_a_cache_file() -> None:
    """A scoped catalogue overwriting the full one would silently truncate it."""
    full = FnoRegistry()
    scoped = FnoRegistry(underlyings={"NIFTY"})
    trimmed = FnoRegistry(max_expiries_per_underlying=2)
    paths = {full._cache_path, scoped._cache_path, trimmed._cache_path}
    assert len(paths) == 3
