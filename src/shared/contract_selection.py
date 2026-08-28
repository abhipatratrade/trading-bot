"""
Contract selection — a spot signal becomes one derivative (Decision 036, Phase B).

Every bucket before this one had a single symbol flowing unchanged from scan to
fill to exit. The F&O buckets break that identity: the scanner and the regime
model reason about NIFTY, and the order goes to one specific strike of one
specific expiry. This module is that seam, built once so it serves any future
strategy rather than being wired into a particular one.

The rule set is CONFIGURATION, not code, because it is a property of the
backtest that validated the strategy (House Rule 7). A run that entered ATM
weeklies at 4+ days to expiry is a different strategy from one that entered 2%
OTM monthlies, and the live system must reproduce whichever was tested rather
than a rule someone chose later. The block lives in ``contracts.yaml`` (or
``contracts_<name>.yaml`` for a Decision 026 named set) in the bucket folder,
alongside the scanner and allocator configs it is paired with.

Deliberately needs NO new API call. Everything here resolves against the scrip
master the registry already holds plus a spot price the runner already fetches,
so contract selection costs nothing extra against a rate-limited account. The
delta rule is the exception and is refused rather than approximated — see
``StrikeRule.DELTA``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, Field, model_validator

from src.core.logging import get_logger
from src.shared.contracts import format_strike

_log = get_logger("shared.contract_selection")


class Instrument(StrEnum):
    FUTURE = "future"
    OPTION = "option"


class OptionSide(StrEnum):
    CALL = "call"
    PUT = "put"
    # Follow the strategy's own direction: a long signal buys a call, a short
    # signal buys a put. The only sane default for a directional strategy
    # ported from spot, where "buy" and "sell" already carry the view.
    DIRECTIONAL = "directional"


class StrikeRule(StrEnum):
    ATM = "atm"
    OTM_PCT = "otm_pct"
    OTM_STEPS = "otm_steps"
    ITM_PCT = "itm_pct"
    # Requires per-strike greeks, which nothing in this repo fetches: Dhan
    # publishes an option-chain endpoint but it is rate-limited far harder than
    # quotes and is not wired. Declared so a config asking for it FAILS rather
    # than silently getting ATM — a 0.30-delta short strangle sized as if it
    # were ATM is a different trade with a different loss profile.
    DELTA = "delta"


class ExpiryRule(StrEnum):
    NEAREST = "nearest"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@runtime_checkable
class ContractLike(Protocol):
    """The shape of one contract, as ``DerivativeContract`` provides it.

    Structural rather than nominal so this module never imports a data-source
    adapter: the live registry satisfies it, and so does a backtester's own
    historical chain row or a test fake.
    """

    symbol: str
    underlying: str
    instrument: str
    expiry: date
    strike: Decimal | None
    option_type: str | None
    lot_size: int


@runtime_checkable
class ContractSource(Protocol):
    """What the selector needs from a contract catalogue.

    A Protocol rather than a hard dependency on ``FnoRegistry`` for the same
    reason ``DhanClient`` takes ``resolve_symbol`` as a callable: this module
    stays testable with a handful of fakes, and the backtester can drive it
    from its own historical chain without importing a live-data adapter.
    """

    def expiries(
        self, underlying: str, *, instrument: str | None = None
    ) -> list[date]: ...

    def chain(
        self, underlying: str, expiry: date, *, option_type: str | None = None
    ) -> Sequence[ContractLike]: ...

    def futures(self, underlying: str) -> Sequence[ContractLike]: ...


class ContractSelectionConfig(BaseModel):
    """The ``contract_selection:`` block of a bucket's ``contracts.yaml``."""

    instrument: Instrument
    option_side: OptionSide = OptionSide.DIRECTIONAL
    strike_rule: StrikeRule = StrikeRule.ATM
    # Meaning depends on strike_rule: percent for OTM_PCT / ITM_PCT, a count of
    # listed strikes for OTM_STEPS, a target delta for DELTA. Ignored by ATM.
    strike_value: Decimal = Decimal("0")
    expiry_rule: ExpiryRule = ExpiryRule.NEAREST
    # Days to expiry the contract must still have. The floor is the important
    # one: an option entered at 0-1 DTE is a different instrument from the same
    # strike at 7 DTE, and the pre-expiry square-off (Phase D) needs room to
    # act before the contract dies.
    min_days_to_expiry: int = Field(default=2, ge=0)
    max_days_to_expiry: int | None = Field(default=None, ge=1)
    # Reserved for Phase C, when a quote feed exists. Declared here so the
    # backtest's liquidity filter has somewhere honest to live meanwhile.
    min_open_interest: int = Field(default=0, ge=0)
    min_volume: int = Field(default=0, ge=0)
    # Where the strategy's signal comes from. Not read by this module — the
    # runner routes on it — but it belongs in the same block because it is the
    # other half of "which series does this strategy actually look at?", and
    # splitting the two across files is how they drift.
    signal_source: str = Field(default="underlying", pattern="^(underlying|contract)$")

    @model_validator(mode="after")
    def _check_rule_inputs(self) -> ContractSelectionConfig:
        if self.strike_rule is StrikeRule.DELTA:
            raise ValueError(
                "strike_rule 'delta' needs per-strike greeks, which this repo "
                "does not fetch. Use atm / otm_pct / otm_steps / itm_pct, or "
                "wire an option-chain feed first."
            )
        needs_value = {
            StrikeRule.OTM_PCT,
            StrikeRule.ITM_PCT,
            StrikeRule.OTM_STEPS,
        }
        if self.strike_rule in needs_value and self.strike_value <= 0:
            raise ValueError(
                f"strike_rule {self.strike_rule.value!r} needs a positive "
                "strike_value"
            )
        if (
            self.max_days_to_expiry is not None
            and self.max_days_to_expiry < self.min_days_to_expiry
        ):
            raise ValueError(
                "max_days_to_expiry must be >= min_days_to_expiry"
            )
        return self


def load_contract_selection(path: Path) -> ContractSelectionConfig:
    """Load and validate one bucket's ``contracts.yaml``. Fail-fast.

    Accepts the block either at the top level or nested under a
    ``contract_selection:`` key, so the file reads naturally either way.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict) and "contract_selection" in raw:
        raw = raw["contract_selection"]
    return ContractSelectionConfig.model_validate(raw)


@dataclass(frozen=True, slots=True)
class Selection:
    """The outcome of asking for a contract.

    ``contract`` is None when no contract qualified, and ``reason`` then says
    why in the vocabulary the audit log will carry. A miss is a normal
    outcome — an underlying can genuinely have no expiry inside the window —
    so it is a value, not an exception.
    """

    underlying: str
    contract: ContractLike | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.contract is not None


class ContractSelector:
    """Turns ``(underlying, spot, side)`` into one contract, deterministically.

    Determinism is the whole requirement: the same inputs and the same scrip
    master must always yield the same contract, or a live fill cannot be
    compared against the backtest that justified it. Every tie-break below is
    therefore total — nearest strike ties break to the LOWER strike, and
    expiry ordering is by date — rather than left to dict ordering.
    """

    def __init__(
        self, source: ContractSource, config: ContractSelectionConfig
    ) -> None:
        self._source = source
        self._config = config

    @property
    def config(self) -> ContractSelectionConfig:
        return self._config

    def select(
        self,
        underlying: str,
        *,
        spot: Decimal,
        side: str,
        on: date,
    ) -> Selection:
        """Pick the contract for one signal.

        Args:
            underlying: the name the scanner and regime model reasoned about.
            spot: its current price, for strike placement. Only consulted for
                options; a future needs no strike.
            side: "buy" or "sell" — the strategy's direction, which decides
                CALL vs PUT under ``OptionSide.DIRECTIONAL``.
            on: today, for the days-to-expiry window.
        """
        cfg = self._config
        if cfg.instrument is Instrument.FUTURE:
            return self._select_future(underlying, on=on)
        return self._select_option(underlying, spot=spot, side=side, on=on)

    # ── futures ─────────────────────────────────────────────────────────
    def _select_future(self, underlying: str, *, on: date) -> Selection:
        candidates = [
            c
            for c in self._source.futures(underlying)
            if self._within_window(c.expiry, on)
        ]
        if not candidates:
            return Selection(underlying, None, self._no_expiry_reason(on))
        # Front month first — the liquid one.
        return Selection(
            underlying, min(candidates, key=lambda c: c.expiry)
        )

    # ── options ─────────────────────────────────────────────────────────
    def _select_option(
        self, underlying: str, *, spot: Decimal, side: str, on: date
    ) -> Selection:
        if spot <= 0:
            return Selection(
                underlying, None, "no spot price for strike placement"
            )

        option_type = self._option_type_for(side)
        expiry = self._pick_expiry(underlying, on=on)
        if expiry is None:
            return Selection(underlying, None, self._no_expiry_reason(on))

        chain = list(self._source.chain(underlying, expiry, option_type=option_type))
        chain = [c for c in chain if c.strike is not None]
        if not chain:
            return Selection(
                underlying,
                None,
                f"no {option_type} strikes listed for expiry {expiry}",
            )

        contract = self._pick_strike(chain, spot=spot, option_type=option_type)
        if contract is None:
            return Selection(
                underlying,
                None,
                f"no strike satisfied {self._config.strike_rule.value}",
            )
        return Selection(underlying, contract)

    def _option_type_for(self, side: str) -> str:
        cfg = self._config
        if cfg.option_side is OptionSide.CALL:
            return "CE"
        if cfg.option_side is OptionSide.PUT:
            return "PE"
        # DIRECTIONAL — the strategy's own view picks the leg.
        return "CE" if side.lower() == "buy" else "PE"

    def _pick_strike(
        self, chain: list[ContractLike], *, spot: Decimal, option_type: str
    ) -> ContractLike | None:
        cfg = self._config
        # Total order, so ties never depend on iteration order.
        ordered = sorted(chain, key=_strike_of)

        if cfg.strike_rule is StrikeRule.ATM:
            return self._nearest_to(ordered, spot)

        if cfg.strike_rule in (StrikeRule.OTM_PCT, StrikeRule.ITM_PCT):
            pct = cfg.strike_value / Decimal("100")
            # OTM is away from the money in the direction the option is WRONG:
            # above spot for a call, below for a put. ITM is the mirror.
            up = (option_type == "CE") == (cfg.strike_rule is StrikeRule.OTM_PCT)
            target = spot * (Decimal("1") + pct if up else Decimal("1") - pct)
            return self._nearest_to(ordered, target)

        if cfg.strike_rule is StrikeRule.OTM_STEPS:
            atm = self._nearest_to(ordered, spot)
            if atm is None:
                return None
            i = ordered.index(atm)
            step = int(cfg.strike_value)
            j = i + step if option_type == "CE" else i - step
            if not 0 <= j < len(ordered):
                return None  # the ladder does not extend that far
            return ordered[j]

        return None  # DELTA is rejected at config load; unreachable in practice

    @staticmethod
    def _nearest_to(
        ordered: list[ContractLike], target: Decimal
    ) -> ContractLike | None:
        """Closest listed strike, ties to the LOWER one.

        The tie-break matters more than it looks: index ladders are evenly
        spaced, so a spot sitting exactly between two strikes is not a rare
        edge case, and "whichever the sort happened to yield" would make the
        same signal pick different contracts on different runs.
        """
        if not ordered:
            return None
        return min(ordered, key=lambda c: (abs(_strike_of(c) - target), _strike_of(c)))

    # ── expiry ──────────────────────────────────────────────────────────
    def _pick_expiry(self, underlying: str, *, on: date) -> date | None:
        cfg = self._config
        listed = sorted(self._source.expiries(underlying))
        eligible = [e for e in listed if self._within_window(e, on)]
        if not eligible:
            return None

        if cfg.expiry_rule is ExpiryRule.NEAREST:
            return eligible[0]

        monthlies = monthly_expiries(listed)
        if cfg.expiry_rule is ExpiryRule.MONTHLY:
            monthly = [e for e in eligible if e in monthlies]
            return monthly[0] if monthly else None

        # WEEKLY: the nearest expiry that is NOT its month's last. Stock F&O
        # lists no weeklies at all, so falling back to the monthly is the
        # difference between "this strategy also works on stocks" and "this
        # strategy silently trades nothing there".
        weekly = [e for e in eligible if e not in monthlies]
        if weekly:
            return weekly[0]
        monthly = [e for e in eligible if e in monthlies]
        return monthly[0] if monthly else None

    def _within_window(self, expiry: date, on: date) -> bool:
        dte = (expiry - on).days
        cfg = self._config
        if dte < cfg.min_days_to_expiry:
            return False
        return cfg.max_days_to_expiry is None or dte <= cfg.max_days_to_expiry

    def _no_expiry_reason(self, on: date) -> str:  # noqa: ARG002
        cfg = self._config
        window = f">= {cfg.min_days_to_expiry}d"
        if cfg.max_days_to_expiry is not None:
            window += f" and <= {cfg.max_days_to_expiry}d"
        return f"no {cfg.expiry_rule.value} expiry in window ({window} to expiry)"


def _strike_of(contract: ContractLike) -> Decimal:
    """Strike as a definite Decimal.

    ``ContractLike.strike`` is Optional because a FUTURE has none, but every
    caller of this has already filtered the chain to options. The default keeps
    the sort total rather than raising on a shape that cannot occur.
    """
    return contract.strike if contract.strike is not None else Decimal("0")


def monthly_expiries(listed: Sequence[date]) -> set[date]:
    """The LAST listed expiry in each calendar month.

    Derived from what is actually listed rather than from a calendar rule,
    because the rule is not stable: NSE has moved its expiry weekday more than
    once, and BSE uses a different one again. Whatever the exchange lists last
    in a month IS that month's monthly contract.
    """
    last: dict[tuple[int, int], date] = {}
    for e in listed:
        key = (e.year, e.month)
        if key not in last or e > last[key]:
            last[key] = e
    return set(last.values())


def contract_hint(contract: ContractLike) -> dict[str, object]:
    """Audit payload for a selected contract.

    Rides the existing ``hint`` -> ``_entry_extra`` -> ``Trade.extra`` JSONB
    path that already carries ``stop_distance`` and ``signal_price``, so no
    migration is needed to record which contract a signal actually resolved to.

    Recording this is not bookkeeping for its own sake: without it, a fill on
    ``NIFTY-20260908-23150-CE`` cannot be traced back to the rule that chose it,
    and "why did it pick that strike?" becomes unanswerable after the fact.
    """
    out: dict[str, object] = {}
    for field_name in ("symbol", "underlying", "instrument", "security_id"):
        value = getattr(contract, field_name, None)
        if value is not None:
            out[f"contract_{field_name}" if field_name != "symbol" else "contract"] = str(
                value
            )
    expiry = getattr(contract, "expiry", None)
    if expiry is not None:
        out["contract_expiry"] = expiry.isoformat()
    strike = getattr(contract, "strike", None)
    if strike is not None:
        out["contract_strike"] = format_strike(strike)
    for field_name in ("option_type", "lot_size"):
        value = getattr(contract, field_name, None)
        if value is not None:
            out[f"contract_{field_name}"] = str(value)
    return out
