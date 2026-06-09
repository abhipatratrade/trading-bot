"""
Position-size allocator.

Per PPTX slide 4(d) + Decision 015, sizing is:

    suggested_notional_inr = bucket.capital_inr
                              * kelly_fraction(μ, σ)
                              * fractional_kelly
                              * regime_multiplier
                              * leverage

If ``suggested_notional_inr > bucket.available_balance_inr``, skip the
trade entirely (do not partially-fill). The insufficient-balance rule is
intentional: Kelly tells you the right size; under-deploying ruins the
edge profile, so we'd rather sit out and wait for capital to free up.

The sizer also enforces the dedup gate: if a position is already open for
(bucket_id, strategy_name, symbol), skip with ``SKIPPED_DEDUP``.

Every call writes a ``SizingSnapshot`` row — placed or skipped — for
audit (Decision 008's "audit every decision" rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.core.db import session_scope
from src.core.logging import get_logger
from src.core.models import (
    AuditEventType,
    AuditLog,
    BucketState,
    MarketRegime,
    Position,
    PositionSide,
    SizingDecision,
    SizingSnapshot,
)
from src.shared.allocator.caps import apply_aggregate_cap, apply_per_symbol_cap
from src.shared.allocator.kelly import fractional_kelly as scale_kelly
from src.shared.allocator.kelly import kelly_fraction
from src.shared.bucket import Bucket

_log = get_logger("shared.allocator.sizer")


# ---------------------------------------------------------------------------
# allocator.yaml schema
# ---------------------------------------------------------------------------
class KellySymbolStats(BaseModel):
    mu_per_period: Decimal
    sigma_per_period: Decimal = Field(gt=0)


class AllocatorConfig(BaseModel):
    fractional_kelly: Decimal = Field(gt=0, le=1, default=Decimal("0.25"))
    per_symbol_cap: Decimal = Field(gt=0, le=1, default=Decimal("0.30"))
    aggregate_cap: Decimal = Field(gt=0, le=1, default=Decimal("1.00"))
    default_for_unknown: KellySymbolStats | None = None
    regime_multipliers: dict[MarketRegime, Decimal] = Field(
        default_factory=lambda: {
            MarketRegime.BEAR: Decimal("0"),
            MarketRegime.NEUTRAL: Decimal("0.5"),
            MarketRegime.BULL: Decimal("1.0"),
        }
    )
    stats: dict[str, KellySymbolStats] = Field(default_factory=dict)


def load_allocator_config(path: Path) -> AllocatorConfig:
    """Load and validate ``allocator.yaml`` for one bucket. Fail-fast."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AllocatorConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Sizing API
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SizingResult:
    """Per-symbol sizing outcome."""

    symbol: str
    decision: SizingDecision
    contracts: Decimal = Decimal("0")
    suggested_notional_inr: Decimal = Decimal("0")
    mu: Decimal | None = None
    sigma: Decimal | None = None
    reason: str | None = None


def size_positions(
    *,
    bucket: Bucket,
    strategy_name: str,
    candidates: list[str],
    mark_prices_inr: dict[str, Decimal],
    regime: MarketRegime | None,
    config: AllocatorConfig,
) -> dict[str, SizingResult]:
    """Compute per-symbol sizing for one strategy in one bucket.

    Args:
        bucket: the materialised bucket (capital + broker + leverage cap).
        strategy_name: the strategy producing these candidates.
        candidates: symbols the scanner + master gate let through.
        mark_prices_inr: most recent mark price per candidate, in INR
            (already converted from USD if needed).
        regime: current bucket regime, or None if regime is disabled.
        config: validated ``allocator.yaml`` for this bucket.

    Returns:
        {symbol: SizingResult} for every input candidate. Caller iterates
        and places orders only where ``decision == PLACED``.
    """
    # Pull current bucket state + held positions in one tx.
    held: set[str]
    available_inr: Decimal
    with session_scope() as session:
        state = session.execute(
            select(BucketState).where(BucketState.bucket_id == bucket.id)
        ).scalar_one_or_none()
        if state is None:
            raise RuntimeError(
                f"BucketState row missing for bucket_id={bucket.id!r}. "
                "Did migration 0002 seed run?"
            )
        available_inr = state.available_balance_inr

        held_rows = session.execute(
            select(Position.symbol).where(
                Position.bucket_id == bucket.id,
                Position.strategy_name == strategy_name,
                Position.side != PositionSide.FLAT,
                Position.quantity > 0,
            )
        ).all()
        held = {r[0] for r in held_rows}

    regime_mult = (
        config.regime_multipliers.get(regime, Decimal("0"))
        if regime is not None
        else Decimal("1")
    )

    # ---- compute raw weights ---------------------------------------------
    raw_weights: dict[str, Decimal] = {}
    kelly_inputs: dict[str, KellySymbolStats] = {}
    for sym in candidates:
        if sym in held:
            continue  # dedup-gated; recorded later
        stats = config.stats.get(sym, config.default_for_unknown)
        if stats is None:
            continue
        kelly_inputs[sym] = stats
        full = kelly_fraction(stats.mu_per_period, stats.sigma_per_period)
        scaled = scale_kelly(full, config.fractional_kelly)
        raw_weights[sym] = scaled * regime_mult

    # ---- apply caps ------------------------------------------------------
    capped = apply_per_symbol_cap(raw_weights, config.per_symbol_cap)
    capped = apply_aggregate_cap(capped, config.aggregate_cap)

    # ---- per-symbol decision ---------------------------------------------
    results: dict[str, SizingResult] = {}
    leverage = bucket.config.leverage_max
    capital = bucket.config.capital_inr

    for sym in candidates:
        if sym in held:
            results[sym] = SizingResult(
                symbol=sym,
                decision=SizingDecision.SKIPPED_DEDUP,
                reason="position already open for this (bucket, strategy, symbol)",
            )
            continue

        stats = kelly_inputs.get(sym)
        if stats is None:
            results[sym] = SizingResult(
                symbol=sym,
                decision=SizingDecision.SKIPPED_OTHER,
                reason="no μ/σ stats in allocator.yaml and no default",
            )
            continue

        weight = capped.get(sym, Decimal("0"))
        suggested_notional = capital * weight * leverage

        if weight <= 0:
            results[sym] = SizingResult(
                symbol=sym,
                decision=SizingDecision.SKIPPED_NEGATIVE_EDGE,
                suggested_notional_inr=Decimal("0"),
                mu=stats.mu_per_period,
                sigma=stats.sigma_per_period,
                reason="kelly fraction ≤ 0 (μ ≤ 0 or regime multiplier 0)",
            )
            continue

        if suggested_notional > available_inr:
            results[sym] = SizingResult(
                symbol=sym,
                decision=SizingDecision.SKIPPED_INSUFFICIENT,
                suggested_notional_inr=suggested_notional,
                mu=stats.mu_per_period,
                sigma=stats.sigma_per_period,
                reason=f"suggested {suggested_notional} > available {available_inr}",
            )
            continue

        price = mark_prices_inr.get(sym)
        if price is None or price <= 0:
            results[sym] = SizingResult(
                symbol=sym,
                decision=SizingDecision.SKIPPED_OTHER,
                suggested_notional_inr=suggested_notional,
                reason="missing or non-positive mark price",
            )
            continue

        contracts = (suggested_notional / price).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        )
        if contracts < 1:
            results[sym] = SizingResult(
                symbol=sym,
                decision=SizingDecision.SKIPPED_OTHER,
                suggested_notional_inr=suggested_notional,
                reason="rounded to 0 contracts",
            )
            continue

        results[sym] = SizingResult(
            symbol=sym,
            decision=SizingDecision.PLACED,
            contracts=contracts,
            suggested_notional_inr=suggested_notional,
            mu=stats.mu_per_period,
            sigma=stats.sigma_per_period,
        )

    # ---- persist audit ----------------------------------------------------
    with session_scope() as session:
        for sym, res in results.items():
            stats = kelly_inputs.get(sym)
            session.add(
                SizingSnapshot(
                    bucket_id=bucket.id,
                    strategy_name=strategy_name,
                    symbol=sym,
                    regime=regime,
                    regime_multiplier=regime_mult,
                    fractional_kelly=config.fractional_kelly,
                    kelly_inputs=(
                        {
                            "mu": str(stats.mu_per_period),
                            "sigma": str(stats.sigma_per_period),
                        }
                        if stats
                        else None
                    ),
                    suggested_notional_inr=res.suggested_notional_inr or None,
                    available_balance_inr=available_inr,
                    contracts=res.contracts or None,
                    mark_price=mark_prices_inr.get(sym),
                    decision=res.decision,
                    reason=res.reason,
                )
            )
        session.add(
            AuditLog(
                strategy_id=bucket.id,
                event_type=AuditEventType.SIZING_DECISION,
                message=(
                    f"sized {len(results)} candidates for "
                    f"{strategy_name}@{bucket.id}: "
                    f"{sum(1 for r in results.values() if r.decision == SizingDecision.PLACED)} placed"
                ),
                payload={
                    "bucket_id": bucket.id,
                    "strategy_name": strategy_name,
                    "regime": regime.value if regime else None,
                    "regime_multiplier": str(regime_mult),
                    "decisions": {s: r.decision.value for s, r in results.items()},
                },
            )
        )

    _log.info(
        "sizing_complete",
        bucket_id=bucket.id,
        strategy_name=strategy_name,
        regime=regime.value if regime else None,
        placed=sum(1 for r in results.values() if r.decision == SizingDecision.PLACED),
        candidates=len(candidates),
    )
    return results
