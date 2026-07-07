"""
Position-size allocator.

Per PPTX slide 4(d) + Decision 015 (clarification 018), sizing is:

    required_margin_inr   = bucket.capital_inr
                            * kelly_fraction(μ, σ)
                            * fractional_kelly
                            * regime_multiplier
    suggested_notional_inr = required_margin_inr * leverage

The PPTX rule "if we have less balance than Kelly suggests, do not
enter" is interpreted as **margin** vs available — the natural
risk-based reading for leveraged perps. Without this clarification the
check ``notional > available`` would cancel any leverage above 1.0x at
typical Kelly weights, which is not what the user intended.

If ``required_margin_inr > bucket.available_balance_inr``, skip the
trade entirely (do not partially-fill). The insufficient-margin rule is
intentional: Kelly tells you the right size; under-deploying ruins the
edge profile, so we'd rather sit out and wait for capital to free up.

The sizer also enforces the dedup gate: if a position is already open for
(bucket_id, strategy_name, symbol), skip with ``SKIPPED_DEDUP``.

Every call writes a ``SizingSnapshot`` row — placed or skipped — for
audit (Decision 008's "audit every decision" rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    OrderStatus,
    Position,
    PositionSide,
    SizingDecision,
    SizingSnapshot,
    Trade,
)
from src.shared.allocator.caps import apply_aggregate_cap, apply_per_symbol_cap
from src.shared.allocator.kelly import fractional_kelly as scale_kelly
from src.shared.allocator.kelly import kelly_fraction
from src.shared.bucket import Bucket

_log = get_logger("shared.allocator.sizer")

# Fallback window over which an already-placed-and-not-rejected Trade for
# (bucket, strategy, symbol) blocks new orders when the caller doesn't
# supply a TF-scaled one. 23 hours matches the longterm-crypto daily
# rebalance cadence (one trade per day per symbol).
_DEDUP_TRADE_WINDOW_HOURS: float = 23.0


def dedup_window_hours_for_tf(tf: str) -> float:
    """Dedup window ≈ one strategy bar, with a 1/24 early-rebalance buffer.

    23/24 of the TF: 1d → 23h (the historical hardcode), 1h → 57.5 min,
    5m → 4.8 min. So a strategy can re-enter a symbol on the next bar of
    ITS timeframe, never within the current one. Unparseable tf falls
    back to the legacy 23h (safe-conservative for anything ≥ intraday).
    """
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        tf_seconds = int(tf[:-1]) * units[tf[-1].lower()]
    except (KeyError, ValueError, IndexError):
        return _DEDUP_TRADE_WINDOW_HOURS
    return tf_seconds / 3600 * 23 / 24


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

    # FX + contract sizing — required for correct contracts math when the
    # bucket capital is in one currency (INR) and the broker quotes prices
    # in another (USD per BTC on Delta India). Each perp also has a
    # contract size (e.g. BTCUSD = 0.001 BTC per contract).
    #
    # contracts = floor(notional_inr / (fx_inr_per_usd × mark_price_usd × contract_size))
    #
    # Default fx = 1.0 means "treat capital and mark price in the same
    # unit", which is the legacy behaviour. Real deployments should set
    # an honest USD/INR rate (~84 at time of writing).
    fx_inr_per_usd: Decimal = Field(gt=0, default=Decimal("1"))
    contract_sizes: dict[str, Decimal] = Field(default_factory=dict)
    default_contract_size: Decimal = Field(gt=0, default=Decimal("1"))


def load_allocator_config(path: Path) -> AllocatorConfig:
    """Load and validate ``allocator.yaml`` for one bucket. Fail-fast."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AllocatorConfig.model_validate(raw)


def notional_inr_to_contracts(
    *,
    notional_inr: Decimal,
    mark_price_usd: Decimal,
    symbol: str,
    config: AllocatorConfig,
    contract_size_override: Decimal | None = None,
    fx_override: Decimal | None = None,
) -> Decimal:
    """Convert an INR-denominated target notional into a whole-contract count.

    Pure function. The pricing identity used is:

        price_per_contract_inr = mark_price_usd × contract_size × fx_inr_per_usd

    so a single contract on (say) BTCUSD with mark_price=$63,500 and
    contract_size=0.001 BTC costs ₹5,334 of notional at FX=84. Dividing
    the bucket-allocated notional by that gives the number of contracts
    the broker should receive.

    ``contract_size_override`` / ``fx_override`` carry live venue values
    (Delta ``/v2/products`` contract_value; refreshed USD/INR). None ⇒
    the static ``allocator.yaml`` values (Phase 1c).

    Returns 0 if any input is non-positive (caller decides what to do).
    """
    if notional_inr <= 0 or mark_price_usd <= 0:
        return Decimal("0")
    contract_size = (
        contract_size_override
        if contract_size_override is not None
        else config.contract_sizes.get(symbol, config.default_contract_size)
    )
    fx = fx_override if fx_override is not None else config.fx_inr_per_usd
    price_per_contract_inr = mark_price_usd * contract_size * fx
    if price_per_contract_inr <= 0:
        return Decimal("0")
    return (notional_inr / price_per_contract_inr).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    )


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
    regimes: dict[str, MarketRegime | None],
    config: AllocatorConfig,
    dedup_window_hours: float | None = None,
    contract_sizes_override: dict[str, Decimal] | None = None,
    fx_inr_per_usd_override: Decimal | None = None,
) -> dict[str, SizingResult]:
    """Compute per-symbol sizing for one strategy in one bucket.

    Args:
        bucket: the materialised bucket (capital + broker + leverage cap).
        strategy_name: the strategy producing these candidates.
        candidates: symbols the scanner + master gate let through.
        mark_prices_inr: most recent mark price per candidate, in INR
            (already converted from USD if needed).
        regimes: per-symbol regime label, or None when no Brain
            prediction is available for that symbol. The sizer looks up
            the multiplier per symbol from
            ``config.regime_multipliers``; symbols missing from the
            dict are treated as None (no regime → multiplier 1.0).
        config: validated ``allocator.yaml`` for this bucket.
        dedup_window_hours: how long an active Trade for (bucket,
            strategy, symbol) blocks re-entry. Callers should pass
            ``dedup_window_hours_for_tf(strategy_tf)``; None falls back
            to the legacy 23h.
        contract_sizes_override: live per-symbol contract sizes from the
            venue's product catalogue; symbols missing from the dict use
            the ``allocator.yaml`` table.
        fx_inr_per_usd_override: live USD/INR rate; None uses the
            ``allocator.yaml`` value.

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
        held_positions = {r[0] for r in held_rows}

        # Also dedup against any active Trade in the last DEDUP_TRADE_WINDOW
        # hours. Position rows are only created by the reconciler (every 5
        # min in run_bot.py). Between order placement and the next reconcile
        # sweep, the dedup gate would otherwise miss positions we just
        # opened, causing the bot to fire a duplicate order every tick.
        # See journal logs 2026-06-12T17:25-17:26 for the bug this fixes.
        cutoff = datetime.now(tz=UTC) - timedelta(
            hours=(
                dedup_window_hours
                if dedup_window_hours is not None
                else _DEDUP_TRADE_WINDOW_HOURS
            )
        )
        active_trade_rows = session.execute(
            select(Trade.symbol).where(
                Trade.bucket_id == bucket.id,
                Trade.strategy_name == strategy_name,
                Trade.status.in_(
                    [
                        OrderStatus.PENDING,
                        OrderStatus.OPEN,
                        OrderStatus.PARTIAL,
                        OrderStatus.FILLED,
                    ]
                ),
                Trade.created_at > cutoff,
            ).distinct()
        ).all()
        recent_open_symbols = {r[0] for r in active_trade_rows}

        held = held_positions | recent_open_symbols

    def _regime_mult_for(sym: str) -> Decimal:
        """Per-symbol regime multiplier from the caller-supplied dict.

        Missing entries (no Brain prediction for that symbol) get
        multiplier 1.0 — the sizer doesn't penalise unknown regime.
        """
        r = regimes.get(sym)
        if r is None:
            return Decimal("1")
        return config.regime_multipliers.get(r, Decimal("0"))

    # ---- compute raw weights ---------------------------------------------
    raw_weights: dict[str, Decimal] = {}
    kelly_inputs: dict[str, KellySymbolStats] = {}
    per_symbol_regime_mult: dict[str, Decimal] = {}
    for sym in candidates:
        if sym in held:
            continue  # dedup-gated; recorded later
        stats = config.stats.get(sym, config.default_for_unknown)
        if stats is None:
            continue
        kelly_inputs[sym] = stats
        full = kelly_fraction(stats.mu_per_period, stats.sigma_per_period)
        scaled = scale_kelly(full, config.fractional_kelly)
        rmult = _regime_mult_for(sym)
        per_symbol_regime_mult[sym] = rmult
        raw_weights[sym] = scaled * rmult

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
        # required_margin uses raw weight × capital (no leverage).
        # suggested_notional is the leveraged-up exposure that actually hits the book.
        required_margin = capital * weight
        suggested_notional = required_margin * leverage

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

        if required_margin > available_inr:
            results[sym] = SizingResult(
                symbol=sym,
                decision=SizingDecision.SKIPPED_INSUFFICIENT,
                suggested_notional_inr=suggested_notional,
                mu=stats.mu_per_period,
                sigma=stats.sigma_per_period,
                reason=(
                    f"required margin {required_margin} > available {available_inr}"
                ),
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

        # FX- and contract-size-aware conversion. The arg name
        # `mark_prices_inr` is legacy; values are actually the broker's
        # raw mark price (USD per underlying on Delta India).
        contracts = notional_inr_to_contracts(
            notional_inr=suggested_notional,
            mark_price_usd=price,
            symbol=sym,
            config=config,
            contract_size_override=(
                contract_sizes_override.get(sym)
                if contract_sizes_override
                else None
            ),
            fx_override=fx_inr_per_usd_override,
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
    # SizingSnapshot.regime is now per-symbol (the regime label that was
    # used to size this particular candidate). regime_multiplier likewise.
    with session_scope() as session:
        for sym, res in results.items():
            stats = kelly_inputs.get(sym)
            sym_regime = regimes.get(sym)
            sym_mult = per_symbol_regime_mult.get(sym, Decimal("1"))
            session.add(
                SizingSnapshot(
                    bucket_id=bucket.id,
                    strategy_name=strategy_name,
                    symbol=sym,
                    regime=sym_regime,
                    regime_multiplier=sym_mult,
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
                    "regimes": {
                        s: (r.value if r else None) for s, r in regimes.items()
                    },
                    "decisions": {s: r.decision.value for s, r in results.items()},
                },
            )
        )

    _log.info(
        "sizing_complete",
        bucket_id=bucket.id,
        strategy_name=strategy_name,
        regimes={s: (r.value if r else None) for s, r in regimes.items()},
        placed=sum(1 for r in results.values() if r.decision == SizingDecision.PLACED),
        candidates=len(candidates),
    )
    return results
