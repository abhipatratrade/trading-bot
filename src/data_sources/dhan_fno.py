"""
Dhan NSE F&O contract registry — futures and options (Decision 036, Phase A).

The equity universe in ``dhan.py`` maps ``ticker -> (security_id, segment)``,
one row per name, refreshed monthly. Derivatives cannot reuse that shape: one
underlying explodes into hundreds of contracts differing by expiry, strike and
option type, and the set turns over every week as expiries roll.

This module owns that catalogue. It is deliberately SEPARATE from ``DhanData``
so the memory-tuned equity parse — see ``_UNIVERSE_COLUMNS`` and the 259MB →
166MB note in ``dhan.py`` — is left untouched on a live system.

Four properties of the source data drove the design. Each is measured, not
assumed; re-run ``scripts/fno_registry_audit.py`` to re-verify after a master
change.

1.  **``SYMBOL_NAME`` IS NOT UNIQUE.** It carries only the expiry *month*, so
    every weekly option in a month collides: ``NIFTY-Sep2026-23150-CE`` names
    FIVE different contracts (expiring 2026-09-01/08/15/22/29), each with its
    own ``SECURITY_ID``. Keying on it would silently trade the wrong expiry —
    the worst possible failure for a strategy with a days-to-expiry rule.
    Measured 2026-08-28: 462 ambiguous names covering 2,236 NSE contracts.
    We therefore MINT our own symbol from the tuple the master *does*
    guarantee unique — ``(underlying, expiry, strike, option_type)`` — with
    the expiry written in full: ``NIFTY-20260908-23150-CE``. That grammar
    lives in ``src/shared/contracts.py``, not here, so the sizer's dedup and
    the reconciler's matching read it from the same place this writes it.

2.  **Futures carry sentinels, not nulls.** ``OPTION_TYPE`` is the literal
    ``"XX"`` and ``STRIKE_PRICE`` is ``-0.01``. Both normalise to None here so
    no caller can mistake -0.01 for a strike or "XX" for a leg type.

3.  **Strikes are not always integers.** 1,654 NSE strikes are fractional
    (42.5, 47.5, 52.5 …), so the minted symbol renders the strike through
    ``Decimal.normalize()`` rather than an int cast.

4.  **The segment is big.** NSE D is 74,322 of the master's 197,254 rows.
    Parsing it the way the equity path does — one full frame — would add
    roughly 60MB on a 958MB VM with no swap that has already OOM'd once
    (2026-08-21). This reads in chunks and keeps only the rows asked for, so
    peak memory is bounded by the chunk size, not by the segment.

Cache lifetime is HOURS, not the equity universe's 30 days: a stale F&O cache
is a list of contracts that no longer trade.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from src.brokers.base import ContractSpec
from src.core.logging import get_logger
from src.shared.contracts import contract_symbol

_log = get_logger("data_sources.dhan_fno")

# Same public master the equity universe reads — one artefact, two views.
_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_DEFAULT_CACHE = _DATA_DIR / "dhan_fno_universe.json"

# Expiries roll weekly. A cache older than one session is a catalogue of
# contracts that may no longer trade, so this is hours where the equity
# universe's is 30 days.
_STALE_HOURS = 12.0

# Dhan's exchange-segment enum value for NSE derivatives. A constant rather
# than a literal because it is the one string in this module NOT verified
# against the master — the CSV carries EXCH_ID/SEGMENT, and the mapping to
# Dhan's ORDER-side segment name comes from its API docs. One edit if wrong.
NSE_FNO = "NSE_FNO"

# Segment code for derivatives in the master's own ``SEGMENT`` column.
_DERIVATIVE_SEGMENT = "D"

# Dhan's exchange-segment enum value for MCX commodity derivatives. Same
# caveat as NSE_FNO: it comes from Dhan's API docs, not from the master.
MCX_COMM = "MCX_COMM"

# Only the columns actually consumed. Same discipline as the equity parse:
# every extra column is ~10MB of Python strings across the full file.
_FNO_COLUMNS = [
    "EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SYMBOL",
    "LOT_SIZE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE", "TICK_SIZE",
    "SM_FREEZE_QTY",
]

# Rows per chunk. Small enough that a chunk's frame stays tens of MB, large
# enough that the whole file is ~8 passes rather than hundreds.
_CHUNK_ROWS = 25_000

# ``TICK_SIZE`` is quoted in PAISE, so the master's value is divided by 100.
#
# Read this carefully, because it is the one number here that is INFERRED
# rather than read: the per-contract VALUE comes straight from the master (no
# guesswork), but the paise->rupee DIVISOR is calibrated against a known
# quantity — NSE cash equity ticks at Rs 0.05 and reads ``5.0000`` in this
# column. If that calibration is ever wrong, every tick is wrong by exactly
# 100x, which the first live order would reject loudly rather than fill badly.
#
# It matters more than it looks. Index futures do NOT tick at Rs 0.05:
# measured 2026-08-28, NIFTY and FINNIFTY read 10 (Rs 0.10) and BANKNIFTY and
# NIFTYNXT50 read 20 (Rs 0.20) — so the pre-036 hardcoded Rs 0.05 snap would
# produce an off-tick price on the most liquid contracts in the market, not
# merely on the 39 obscure stock futures that first surfaced this.
_PAISE = Decimal("100")

# Instrument codes in the master's ``INSTRUMENT`` column, per venue.
_FUTURES = frozenset({"FUTIDX", "FUTSTK", "FUTCOM"})
_OPTIONS = frozenset({"OPTIDX", "OPTSTK", "OPTFUT"})
# Stock derivatives are PHYSICALLY SETTLED — an in-the-money contract carried
# past expiry delivers shares at full contract value. Index derivatives are
# cash settled. The distinction is enforced in Phase D; it is surfaced here
# because it is a property of the instrument, not of a strategy.
#
# MCX commodities are NOT listed here and therefore default to "not physically
# settled by instrument code" — which is the wrong default to rely on, because
# several MCX contracts (the metals especially) ARE compulsory-delivery. The
# real gate is the bucket's ``cash_settled_underlyings`` set, which is fail-safe
# in the other direction: anything not explicitly named as cash-settled is
# treated as delivery-risky by ``check_expiry_window``. Instrument code alone
# must never be the thing that decides.
_PHYSICALLY_SETTLED = frozenset({"FUTSTK", "OPTSTK"})


# ── Contract multipliers ────────────────────────────────────────────────
#
# THE FIELD THE SCRIP MASTER DOES NOT CARRY, and the reason this table exists.
#
# On NSE, ``LOT_SIZE`` is both the order-quantity unit AND the number of
# underlying units a lot controls — NIFTY reads 65, an order for one lot is
# quantity 65, and the notional is 65 x price. The two coincide, so nothing
# ever had to tell them apart.
#
# On MCX they do NOT. ``LOT_SIZE`` reads **1** for every commodity contract:
# that is the order-quantity unit (you order quantity=1 for one lot), while the
# units a lot actually controls — 250 mmBtu for Natural Gas Mini — appear
# NOWHERE in the file. Sizing off ``LOT_SIZE`` there computes a notional 250x
# too small, and every margin and Kelly figure derived from it is wrong by the
# same factor.
#
# So the multiplier is carried here, cited, per underlying. An underlying with
# no entry gets ``lot_size`` (the NSE-correct behaviour), which is safe for NSE
# and WRONG for MCX — hence ``MCX_VENUE.require_multiplier``, which refuses to
# load an MCX contract whose multiplier is unknown rather than silently sizing
# it 250x small.
#
# Sources: MCX's own product page (mcxindia.com/products/energy/natural-gas)
# refuses automated fetch, so these are from Zerodha's contract bulletin
# ("Natural Gas Mini 250 mmBtu futures as underlying") corroborated by broker
# contract-spec pages, all read 2026-08-29. CROSS-CHECK AVAILABLE: the tick
# size these sources give (Rs 0.10) matches the master's own TICK_SIZE of 10
# paise for NATGASMINI, so at least one number from the same specification
# reconciles against the authoritative file.
#
# CONFIRM AGAINST A REAL CONTRACT NOTE before this sizes real money. A wrong
# multiplier is not a rounding error; it is a 250x position.
_MCX_MULTIPLIERS: dict[str, Decimal] = {
    "NATGASMINI": Decimal("250"),    # 250 mmBtu
    "NATURALGAS": Decimal("1250"),   # 1,250 mmBtu (the full-size contract)
}


@dataclass(frozen=True, slots=True)
class Venue:
    """One exchange's shape in the scrip master.

    Exists because the master is not uniform: the same columns mean different
    things on NSE and MCX, and the difference is silent rather than an error.
    """

    exchange: str          # EXCH_ID
    segment: str           # SEGMENT
    order_segment: str     # Dhan's exchangeSegment enum value
    multipliers: dict[str, Decimal]
    # When True, an underlying with no multiplier entry is REFUSED rather than
    # defaulted to lot_size. True for MCX, where the default is known-wrong.
    require_multiplier: bool = False


NSE_VENUE = Venue(
    exchange="NSE",
    segment=_DERIVATIVE_SEGMENT,
    order_segment=NSE_FNO,
    multipliers={},
    require_multiplier=False,
)

MCX_VENUE = Venue(
    exchange="MCX",
    segment="M",
    order_segment=MCX_COMM,
    multipliers=_MCX_MULTIPLIERS,
    require_multiplier=True,
)

VENUES = {"NSE": NSE_VENUE, "MCX": MCX_VENUE}

# Sentinels the master uses in place of nulls on futures rows.
_NO_OPTION_TYPE = "XX"


@dataclass(frozen=True, slots=True)
class DerivativeContract:
    """One tradeable NSE derivative.

    ``symbol`` is minted by :func:`contract_symbol` and is this system's
    canonical key — never the master's ambiguous ``SYMBOL_NAME``. It is the
    value that lands in ``Position.symbol`` / ``Trade.symbol`` (String(64);
    the longest possible value here is ~31 chars).
    """

    symbol: str
    security_id: str
    exchange_segment: str
    underlying: str
    instrument: str
    expiry: date
    lot_size: int
    tick_size: Decimal
    freeze_qty: int
    strike: Decimal | None = None
    option_type: str | None = None  # "CE" | "PE"; None on futures
    # Underlying units ONE LOT controls, for notional and margin arithmetic.
    # Equal to ``lot_size`` on NSE, where the master's LOT_SIZE is both the
    # order unit and the contract size. NOT equal on MCX, where LOT_SIZE is 1
    # and the real figure (250 mmBtu for NATGASMINI) is absent from the file —
    # see ``_MCX_MULTIPLIERS``.
    #
    # The distinction is the whole reason this field exists: ORDER QUANTITY is
    # ``lots x lot_size``, while NOTIONAL is ``lots x multiplier x price``.
    multiplier: Decimal = Decimal("0")  # 0 ⇒ fall back to lot_size

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            object.__setattr__(self, "multiplier", Decimal(self.lot_size))

    @property
    def is_option(self) -> bool:
        return self.instrument in _OPTIONS

    @property
    def is_future(self) -> bool:
        return self.instrument in _FUTURES

    @property
    def is_index(self) -> bool:
        return self.instrument in ("FUTIDX", "OPTIDX")

    @property
    def physically_settled(self) -> bool:
        """True when expiry delivers SHARES, not cash.

        The single largest loss vector in the F&O buckets: an ITM stock
        derivative carried past expiry creates a delivery obligation at full
        contract value (median ~Rs 6.7L against a Rs 5L bucket). Phase D turns
        this flag into a mandatory pre-expiry square-off invariant.
        """
        return self.instrument in _PHYSICALLY_SETTLED

    def days_to_expiry(self, on: date) -> int:
        return (self.expiry - on).days

    def spec(self) -> ContractSpec:
        """The venue's trading unit, for the broker adapter."""
        return ContractSpec(
            lot_size=Decimal(self.lot_size),
            tick_size=self.tick_size,
            freeze_qty=Decimal(self.freeze_qty) if self.freeze_qty > 0 else None,
            multiplier=Decimal(self.multiplier or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "exchange_segment": self.exchange_segment,
            "underlying": self.underlying,
            "instrument": self.instrument,
            "expiry": self.expiry.isoformat(),
            "lot_size": self.lot_size,
            "tick_size": str(self.tick_size),
            "freeze_qty": self.freeze_qty,
            "multiplier": str(self.multiplier),
            "strike": str(self.strike) if self.strike is not None else None,
            "option_type": self.option_type,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DerivativeContract:
        strike = raw.get("strike")
        return cls(
            symbol=raw["symbol"],
            security_id=raw["security_id"],
            exchange_segment=raw["exchange_segment"],
            underlying=raw["underlying"],
            instrument=raw["instrument"],
            expiry=date.fromisoformat(raw["expiry"]),
            lot_size=int(raw["lot_size"]),
            tick_size=Decimal(raw["tick_size"]),
            freeze_qty=int(raw["freeze_qty"]),
            multiplier=Decimal(raw.get("multiplier") or 0),
            strike=Decimal(strike) if strike is not None else None,
            option_type=raw.get("option_type"),
        )


class FnoRegistry:
    """Catalogue of NSE derivative contracts, cached to disk.

    Built lazily: nothing is downloaded until a lookup needs it. Construct
    with ``contracts=`` in tests to skip I/O entirely.

    ``underlyings`` scopes the catalogue. Leaving it None keeps all ~74k NSE
    contracts (~15MB resident); passing the bucket's pinned universe is the
    intended production path and cuts that proportionally. The cache filename
    carries a digest of the filter, so two scopes never clobber each other.
    """

    def __init__(
        self,
        contracts: list[DerivativeContract] | None = None,
        *,
        underlyings: Collection[str] | None = None,
        max_expiries_per_underlying: int | None = None,
        cache_path: Path | None = None,
        http: httpx.Client | None = None,
        exchange: str = "NSE",
    ) -> None:
        self._underlyings = frozenset(underlyings) if underlyings else None
        self._max_expiries = max_expiries_per_underlying
        self._exchange = exchange
        self._venue = VENUES.get(exchange, NSE_VENUE)
        self._cache_path = cache_path or _scoped_cache_path(
            self._underlyings, self._max_expiries, exchange
        )
        self._http = http
        self._owns_http = http is None
        self._by_symbol: dict[str, DerivativeContract] | None = None
        self._by_security_id: dict[str, DerivativeContract] | None = None
        if contracts is not None:
            self._index(contracts)

    # ── loading ─────────────────────────────────────────────────────────
    def _index(self, contracts: list[DerivativeContract]) -> None:
        """Build the lookup maps, shouting if the minted key is not unique.

        ``(underlying, expiry, strike, option_type)`` is unique across all
        74,322 NSE rows (verified 2026-08-28), so a collision here means the
        master's shape changed under us — a new instrument class, a second
        exchange folded into the same scope, or a settlement variant sharing a
        strike. Left silent it would look exactly like the ``SYMBOL_NAME`` bug
        this registry exists to avoid: one contract quietly shadowing another,
        with the loser un-tradeable and the winner arbitrary.
        """
        by_symbol: dict[str, DerivativeContract] = {}
        collisions: list[str] = []
        for c in contracts:
            if c.symbol in by_symbol:
                collisions.append(c.symbol)
            by_symbol[c.symbol] = c
        if collisions:
            _log.warning(
                "fno_symbol_collision",
                count=len(collisions),
                sample=sorted(set(collisions))[:5],
            )
        self._by_symbol = by_symbol
        self._by_security_id = {c.security_id: c for c in contracts}

    @property
    def contracts(self) -> list[DerivativeContract]:
        if self._by_symbol is None:
            self._index(self._load())
        assert self._by_symbol is not None
        return list(self._by_symbol.values())

    def _ensure(self) -> dict[str, DerivativeContract]:
        if self._by_symbol is None:
            self._index(self._load())
        assert self._by_symbol is not None
        return self._by_symbol

    def _load(self, force: bool = False) -> list[DerivativeContract]:
        cache = self._cache_path
        if cache.exists() and not force:
            age_hours = (time.time() - cache.stat().st_mtime) / 3600
            if age_hours < _STALE_HOURS:
                try:
                    raw = json.loads(cache.read_text(encoding="utf-8"))
                    return [DerivativeContract.from_dict(r) for r in raw]
                except (OSError, ValueError, KeyError):
                    _log.warning("fno_cache_unreadable", path=str(cache))
        contracts = self._fetch()
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps([c.to_dict() for c in contracts]), encoding="utf-8"
            )
        except OSError:
            _log.warning("fno_cache_write_failed", path=str(cache))
        return contracts

    def refresh(self) -> list[DerivativeContract]:
        """Force a re-download, bypassing the cache. Returns the new set."""
        contracts = self._load(force=True)
        self._index(contracts)
        return contracts

    # ── parsing ─────────────────────────────────────────────────────────
    def _fetch(self) -> list[DerivativeContract]:
        """Download + chunk-parse the master's derivative segment.

        Chunked deliberately: see property 4 in the module docstring. The
        response bytes are held once (~33MB) and each chunk's frame is
        released before the next is read, so peak is bytes + one chunk rather
        than bytes + a 74k-row frame.
        """
        import pandas as pd

        http = self._http or httpx.Client(timeout=60.0)
        try:
            _log.info("fno_universe_fetch_start", url=_SCRIP_MASTER_URL)
            resp = http.get(_SCRIP_MASTER_URL, timeout=60.0)
            resp.raise_for_status()
            payload = resp.content
        finally:
            if self._http is None:
                http.close()

        from io import BytesIO

        today = date.today()
        out: list[DerivativeContract] = []
        reader = pd.read_csv(
            BytesIO(payload),
            dtype=str,
            usecols=_FNO_COLUMNS,
            low_memory=False,
            chunksize=_CHUNK_ROWS,
        )
        for chunk in reader:
            rows = chunk[
                (chunk["SEGMENT"] == self._venue.segment)
                & (chunk["EXCH_ID"] == self._venue.exchange)
            ]
            if self._underlyings is not None:
                rows = rows[rows["UNDERLYING_SYMBOL"].isin(self._underlyings)]
            if rows.empty:
                continue
            out.extend(self._rows_to_contracts(rows, today))

        if self._max_expiries is not None:
            out = _keep_nearest_expiries(out, self._max_expiries)

        _log.info(
            "fno_universe_resolved",
            contracts=len(out),
            underlyings=len({c.underlying for c in out}),
            scoped=self._underlyings is not None,
        )
        return out

    def _rows_to_contracts(self, rows: Any, today: date) -> Iterator[DerivativeContract]:
        """One master row -> one contract, skipping anything unparseable.

        Skips rather than raises: a single malformed row in a 74k-row public
        CSV must not take the whole catalogue down, and the count is logged so
        a sudden jump is visible.
        """
        skipped = 0
        unknown_multipliers: set[str] = set()
        for r in rows.itertuples(index=False):
            try:
                expiry = date.fromisoformat(str(r.SM_EXPIRY_DATE).strip()[:10])
                # An already-expired contract is dead weight and a trap: it
                # resolves, so a caller could place an order the venue refuses.
                if expiry < today:
                    continue
                underlying = str(r.UNDERLYING_SYMBOL).strip()
                instrument = str(r.INSTRUMENT).strip()
                if not underlying or instrument not in (_FUTURES | _OPTIONS):
                    continue

                raw_type = str(r.OPTION_TYPE).strip().upper()
                option_type = (
                    raw_type if raw_type in ("CE", "PE") else None
                )
                strike: Decimal | None = None
                if option_type is not None:
                    parsed = Decimal(str(r.STRIKE_PRICE))
                    # Futures rows carry -0.01; an option must have a real one.
                    if parsed <= 0:
                        continue
                    strike = parsed
                elif raw_type not in (_NO_OPTION_TYPE, "", "NAN", "NONE"):
                    # An unrecognised leg type is not a future — skip loudly
                    # rather than silently trading it as one.
                    skipped += 1
                    continue

                lot_size = int(Decimal(str(r.LOT_SIZE)))
                if lot_size <= 0:
                    continue
                tick = (Decimal(str(r.TICK_SIZE)) / _PAISE).normalize()
                if tick <= 0:
                    continue
                freeze = int(Decimal(str(r.SM_FREEZE_QTY or 0)))

                venue = self._venue
                multiplier = venue.multipliers.get(underlying)
                if multiplier is None:
                    if venue.require_multiplier:
                        # REFUSE rather than default. On MCX the default is
                        # known-wrong by the contract size (250x for gas), and
                        # a silently 250x-small notional would size a position
                        # 250x too large once margin is fitted to it.
                        unknown_multipliers.add(underlying)
                        continue
                    multiplier = Decimal(lot_size)

                yield DerivativeContract(
                    symbol=contract_symbol(
                        underlying, expiry, strike=strike, option_type=option_type
                    ),
                    security_id=str(r.SECURITY_ID).strip(),
                    exchange_segment=venue.order_segment,
                    underlying=underlying,
                    instrument=instrument,
                    expiry=expiry,
                    lot_size=lot_size,
                    tick_size=tick,
                    freeze_qty=max(freeze, 0),
                    strike=strike,
                    option_type=option_type,
                    multiplier=multiplier,
                )
            except (ArithmeticError, TypeError, ValueError):
                skipped += 1
        if skipped:
            _log.warning("fno_rows_skipped", count=skipped)
        if unknown_multipliers:
            # ERROR, not warning: on a venue where the multiplier is required,
            # every contract of that underlying is unusable, and a quiet skip
            # would look exactly like "that commodity is not listed".
            _log.error(
                "contract_multiplier_unknown_underlying_refused",
                exchange=self._venue.exchange,
                underlyings=sorted(unknown_multipliers),
            )

    # ── lookup ──────────────────────────────────────────────────────────
    def get(self, symbol: str) -> DerivativeContract | None:
        return self._ensure().get(symbol)

    def by_security_id(self, security_id: str) -> DerivativeContract | None:
        """Reverse lookup — the join that survives a symbol-format surprise.

        Dhan's position/order payloads report a ``tradingSymbol`` whose exact
        F&O format is unverified against a live account. ``securityId`` is on
        every payload and is unique, so the reconciler can match on it whatever
        the string turns out to look like.
        """
        self._ensure()
        assert self._by_security_id is not None
        return self._by_security_id.get(str(security_id))

    def resolve(self, symbol: str) -> tuple[str, str]:
        """Canonical symbol -> ``(security_id, exchange_segment)``.

        Signature-compatible with ``DhanData.resolve`` so one ``ResolveSymbol``
        callable can serve both cash equity and derivatives.
        """
        c = self.get(symbol)
        if c is None:
            raise ValueError(f"Unknown Dhan F&O contract: {symbol!r}")
        return c.security_id, c.exchange_segment

    def spec(self, symbol: str) -> ContractSpec | None:
        c = self.get(symbol)
        return c.spec() if c is not None else None

    def underlyings(self) -> set[str]:
        return {c.underlying for c in self._ensure().values()}

    def expiries(
        self, underlying: str, *, instrument: str | None = None
    ) -> list[date]:
        """Ascending distinct expiries for an underlying."""
        return sorted(
            {
                c.expiry
                for c in self._ensure().values()
                if c.underlying == underlying
                and (instrument is None or c.instrument == instrument)
            }
        )

    def chain(
        self,
        underlying: str,
        expiry: date,
        *,
        option_type: str | None = None,
    ) -> list[DerivativeContract]:
        """Option contracts for one underlying+expiry, ascending by strike."""
        return sorted(
            (
                c
                for c in self._ensure().values()
                if c.underlying == underlying
                and c.expiry == expiry
                and c.is_option
                and (option_type is None or c.option_type == option_type)
            ),
            key=lambda c: (c.strike or Decimal("0"), c.option_type or ""),
        )

    def futures(self, underlying: str) -> list[DerivativeContract]:
        """Futures on one underlying, ascending by expiry (front month first)."""
        return sorted(
            (
                c
                for c in self._ensure().values()
                if c.underlying == underlying and c.is_future
            ),
            key=lambda c: c.expiry,
        )

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None


def _keep_nearest_expiries(
    contracts: list[DerivativeContract], n: int
) -> list[DerivativeContract]:
    """Trim each underlying to its ``n`` nearest expiries.

    Materially useful only for the index names — NIFTY lists 18 expiries where
    a stock lists 3 — which is why it is opt-in rather than a default.
    """
    keep: dict[str, set[date]] = {}
    for c in contracts:
        keep.setdefault(c.underlying, set()).add(c.expiry)
    nearest = {u: set(sorted(e)[:n]) for u, e in keep.items()}
    return [c for c in contracts if c.expiry in nearest[c.underlying]]


def _scoped_cache_path(
    underlyings: frozenset[str] | None,
    max_expiries: int | None,
    exchange: str,
) -> Path:
    """One cache file per (scope, expiry-window, exchange) combination.

    Without the digest, a scoped registry would overwrite the full one and the
    next full read would silently see a truncated catalogue.
    """
    if underlyings is None and max_expiries is None and exchange == "NSE":
        return _DEFAULT_CACHE
    key = json.dumps(
        {
            "u": sorted(underlyings) if underlyings else None,
            "n": max_expiries,
            "x": exchange,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return _DATA_DIR / f"dhan_fno_universe_{digest}.json"


def cache_age_hours(path: Path | None = None) -> float | None:
    """Age of a registry cache in hours, or None when absent.

    Exposed for the dashboard and session invariants: a registry older than
    one session is a catalogue of contracts that may no longer trade.
    """
    p = path or _DEFAULT_CACHE
    if not p.exists():
        return None
    return (time.time() - p.stat().st_mtime) / 3600
