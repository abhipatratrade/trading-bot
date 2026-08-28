"""
Derivative contract symbol grammar (Decision 036, Phase B).

ONE place that knows how a contract symbol is spelled, so the registry that
mints them, the sizer that dedups on them, the reconciler that matches them and
the backtester that replays them cannot drift apart. A grammar duplicated in
four modules is a grammar that will disagree in three of them.

Deliberately dependency-free — no pandas, no httpx, no DB, no ORM. It sits in
the sizer's hot path and in layers the backtester imports, and it must stay
cheap and importable from anywhere.

The grammar::

    <UNDERLYING>-<YYYYMMDD>-FUT                  NIFTY-20260929-FUT
    <UNDERLYING>-<YYYYMMDD>-<STRIKE>-<CE|PE>     NIFTY-20260908-23150-CE

Why the full date rather than Dhan's own ``SYMBOL_NAME``: that field carries
only the expiry MONTH, so five different NIFTY weeklies share the string
``NIFTY-Sep2026-23150-CE``. See ``src/data_sources/dhan_fno.py``.

**The underlying is matched greedily and the date anchors the parse**, which is
the part that is easy to get wrong. A naive ``symbol.split("-")[0]`` looks
correct until it meets a cash-equity ticker that contains a hyphen — the live
swing-indian universe holds ``NAM-INDIA``, which that shortcut turns into
``NAM``. Every function here treats a non-matching symbol as a plain cash
symbol and returns it unchanged, so cash equity flows through untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# Anchored on the 8-digit expiry and the terminal FUT / CE / PE. The greedy
# ``.+`` for the underlying means a hyphenated underlying (NAM-INDIA) survives
# intact should such a name ever list derivatives; the alternation disambiguates
# an 8-digit strike from the expiry by backtracking.
_SYMBOL_RE = re.compile(
    r"^(?P<underlying>.+)-(?P<expiry>\d{8})-"
    r"(?:FUT|(?P<strike>\d+(?:\.\d+)?)-(?P<option_type>CE|PE))$"
)

_FUTURE_SUFFIX = "FUT"


@dataclass(frozen=True, slots=True)
class ContractKey:
    """The tuple a contract symbol encodes.

    Verified unique across all 74,322 NSE derivative rows (2026-08-28), which
    is what makes it safe to use as this system's identity for a contract.
    """

    underlying: str
    expiry: date
    strike: Decimal | None = None
    option_type: str | None = None  # "CE" | "PE"; None on a future

    @property
    def is_future(self) -> bool:
        return self.option_type is None

    @property
    def symbol(self) -> str:
        return contract_symbol(
            self.underlying,
            self.expiry,
            strike=self.strike,
            option_type=self.option_type,
        )


def contract_symbol(
    underlying: str,
    expiry: date,
    *,
    strike: Decimal | None = None,
    option_type: str | None = None,
) -> str:
    """Mint this system's canonical contract symbol.

    The strike renders through ``Decimal.normalize()`` so ``23150.00000`` and
    ``42.50000`` become ``23150`` and ``42.5`` — stable across scrip-master
    refreshes, and fraction-preserving for the 1,654 half-point NSE strikes an
    int cast would collide with their neighbours.
    """
    stem = f"{underlying}-{expiry:%Y%m%d}"
    if option_type is None or strike is None:
        return f"{stem}-{_FUTURE_SUFFIX}"
    return f"{stem}-{format_strike(strike)}-{option_type}"


def format_strike(strike: Decimal) -> str:
    """``23150.00000`` -> ``23150``; ``42.50000`` -> ``42.5``.

    ``normalize()`` on a whole number can yield exponent notation
    (``2.315E+4``), which is unusable as a key, so whole values are re-quantised
    to a plain integer string.
    """
    n = strike.normalize()
    return str(n.quantize(Decimal("1")) if n == n.to_integral_value() else n)


def parse_contract_symbol(symbol: str) -> ContractKey | None:
    """Split a contract symbol back into its parts, or None if it isn't one.

    None means "this is a plain cash symbol", not "this is malformed" — the
    two namespaces share one column and one resolver, and only the grammar
    tells them apart.
    """
    m = _SYMBOL_RE.match(symbol)
    if m is None:
        return None
    try:
        expiry = date(
            int(m["expiry"][:4]), int(m["expiry"][4:6]), int(m["expiry"][6:])
        )
    except ValueError:
        return None  # 8 digits that aren't a real date
    raw_strike = m["strike"]
    return ContractKey(
        underlying=m["underlying"],
        expiry=expiry,
        strike=Decimal(raw_strike) if raw_strike is not None else None,
        option_type=m["option_type"],
    )


def is_derivative(symbol: str) -> bool:
    return parse_contract_symbol(symbol) is not None


def underlying_of(symbol: str) -> str:
    """The name a symbol is exposure to — itself, for cash equity.

    This is the dedup and risk-aggregation key, and the reason it exists is
    concrete: if the dedup gate keyed on the contract symbol, a strategy
    already short one NIFTY strike would read a second strike on the SAME
    index as an unrelated name and open it, doubling exposure the allocator
    believes it has capped. Two strikes on one underlying are one bet with two
    spellings.

    Returning the input unchanged for a non-derivative is what lets callers use
    this unconditionally: for every cash-equity bucket it is the identity
    function, so there is no F&O branch to forget.
    """
    key = parse_contract_symbol(symbol)
    return key.underlying if key is not None else symbol
