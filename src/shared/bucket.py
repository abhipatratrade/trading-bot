"""
Bucket model + loader.

A "bucket" is a (trading_type × market) pair — e.g. ``longterm-crypto``.
Each bucket has:
- a fixed INR capital allocation (Decision 013),
- a broker adapter,
- a maximum leverage,
- its own regime/scanner/allocator config under ``src/strategies/<type>/<market>/``,
- a Strategy Master CSV listing the strategies that may run in it.

The Bucket *config* (capital, broker, leverage cap) lives in a single
top-level ``buckets.yaml`` at the repo root so it's easy to edit / audit.
The per-bucket *runtime* state (available balance, locked margin) lives in
the ``bucket_state`` Postgres table and is updated by the reconciler.

Six buckets exist by default (matching ``buckets.yaml``):
    longterm-crypto, swing-crypto, scalp-crypto, gambling-crypto,
    longterm-indian, swing-indian.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from src.core.models import BrokerName

# Repo root (..\..\.. relative to this file).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUCKETS_YAML_PATH = _REPO_ROOT / "buckets.yaml"


class TradingType(StrEnum):
    LONGTERM = "longterm"
    SWING = "swing"
    SCALP = "scalp"
    GAMBLING = "gambling"
    # Decision 029 — same-day entry and exit. Distinct from SCALP (which is a
    # crypto high-leverage type) in that positions are opened and squared off
    # inside one NSE session.
    INTRADAY = "intraday"
    # Decision 036 — the two Indian derivative buckets. These name an
    # INSTRUMENT CLASS where every value above names a holding period, which
    # is a real wart and was chosen with it understood: bucket ids parse as
    # ``<type>-<market>``, so keeping derivatives inside this enum means
    # ``futures-indian`` / ``options-indian`` need no change to id parsing,
    # the ``bucket_id`` columns, the dashboard routes, or any existing
    # bucket's identity. The alternative — a third (instrument) axis — is
    # cleaner in the abstract and touches every one of those.
    #
    # The cost, recorded so it is not rediscovered: holding period is no
    # longer expressible for a derivative bucket, so one options bucket holds
    # both an intraday and a swing option strategy, separated only by their
    # strategy_master rows and named scanner sets (Decision 026).
    FUTURES = "futures"
    OPTIONS = "options"


class Market(StrEnum):
    CRYPTO = "crypto"
    INDIAN = "indian"


class BucketConfig(BaseModel):
    """Per-bucket config block from ``buckets.yaml``."""

    capital_inr: Decimal = Field(ge=0)
    broker: BrokerName
    leverage_max: Decimal = Field(ge=1)
    enabled: bool = True
    # Which broker (sub-)account this bucket trades on (Decision 019). Crypto
    # buckets each get their own Delta India sub-account so positions,
    # leverage, and margin are isolated. ``default`` reuses the original keys.
    account_ref: str = "default"
    # Decision 022 — broker-side protective stop distance as a percent of
    # entry price. Every open position gets an exchange-resident reduce-only
    # stop-market order at this distance, so a max loss holds even when the
    # bot/VM is down. None ⇒ no broker-side stop for this bucket.
    stop_loss_pct: Decimal | None = Field(default=None, gt=0, lt=100)
    # Decision 034 — carry the protective stop ON the entry order (Dhan Super
    # Order) instead of resting it separately after the fill. Per-BUCKET, not
    # just per-process: the whole rollout plan is "enable for one bucket, watch
    # the first entry, keep the Decision 022 sweep behind it", and a global
    # switch cannot express that — flipping it would arm swing-indian and
    # intraday-indian in the same instant, on one shared live account, with no
    # rehearsal. Both this AND the ``attached_stops_enabled`` setting must be
    # true, so the env var stays a process-wide master kill.
    attached_stops: bool = False
    # Decision 029 — per-bucket entry window (IST "HH:MM"), consumed by
    # ``market_calendar.nse_session`` via BucketRunner. Defaults reproduce the
    # original module-level constants, so every pre-existing bucket is
    # unchanged; intraday-indian overrides the start to 09:30.
    entry_start: str = "09:45"
    entry_end: str = "10:30"
    # Broker margin/product mode for this bucket's orders, when the venue has
    # one (Decision 029). Dhan cash equity: MTF (funded delivery, swing) vs
    # INTRADAY (MIS, same-day). It is per-bucket rather than per-broker because
    # Dhan has a single account, so both Indian buckets share one adapter.
    # None ⇒ the adapter's own default. Crypto buckets leave it unset.
    product: str | None = None
    # Product to retry on when ``product`` is rejected as ineligible for this
    # scrip (Decision 029, amended 2026-07-27 by user decision). intraday-indian
    # sets CNC: a scrip Dhan grants no MIS on is traded 1x delivery rather than
    # skipped. The retry is ALWAYS sized 1x — the runner passes the cash-
    # affordable quantity — so the bucket never spends more margin than budgeted.
    # None ⇒ no fallback; an ineligible scrip fails loudly.
    fallback_product: str | None = None
    # Decision 032 — how often the runner takes a full pipeline pass, in
    # seconds. None ⇒ derived from the fastest timeframe in the bucket (see
    # ``BucketRunner``). Set it when a bucket's cadence must be pinned
    # independently of its regime/strategy TFs: swing-indian runs a 1h
    # strategy under a 1d regime model and needs to act on a 1h close
    # promptly, not up to 15 minutes later.
    tick_interval_seconds: int | None = Field(default=None, ge=10, le=3600)
    # Decision 032 — annual financing rate the broker charges on the FUNDED
    # portion of a carried position (Dhan MTF ≈ 14.6%/yr). The reconciler
    # subtracts it from realized P&L per calendar day held. None ⇒ the
    # product is unfunded (CNC, MIS, crypto) and nothing is charged.
    carry_interest_apr: Decimal | None = Field(default=None, ge=0, lt=1)


class BucketsConfig(BaseModel):
    """Top-level ``buckets.yaml``."""

    buckets: dict[str, BucketConfig]


@dataclass(frozen=True, slots=True)
class Bucket:
    """Materialised bucket — config + filesystem paths.

    The ``id`` field is the canonical bucket identifier used throughout
    the system (e.g. as ``bucket_id`` in DB rows). It's always
    ``f"{trading_type}-{market}"``.
    """

    id: str
    trading_type: TradingType
    market: Market
    config: BucketConfig
    folder: Path

    @property
    def scanner_yaml_path(self) -> Path:
        return self.folder / "scanner.yaml"

    @property
    def regime_yaml_path(self) -> Path:
        return self.folder / "regime.yaml"

    @property
    def allocator_yaml_path(self) -> Path:
        return self.folder / "allocator.yaml"

    # Decision 026 — named scanner sets. Each name maps to a
    # scanner_<name>.yaml + allocator_<name>.yaml pair in the bucket
    # folder; "" is the default pair above. Strategies pick a set via the
    # optional ``scanner`` column in strategy_master.csv.
    def scanner_yaml_path_for(self, scanner: str = "") -> Path:
        return (
            self.folder / f"scanner_{scanner}.yaml"
            if scanner
            else self.scanner_yaml_path
        )

    def allocator_yaml_path_for(self, scanner: str = "") -> Path:
        return (
            self.folder / f"allocator_{scanner}.yaml"
            if scanner
            else self.allocator_yaml_path
        )

    # Decision 036 — how a signal on an underlying becomes one derivative
    # contract. Follows the Decision 026 named-set pattern so a bucket running
    # two scanner sets can give each its own strike/expiry rule.
    #
    # OPTIONAL, unlike the three above: absence means "this bucket trades the
    # symbol the scanner produced", which is every cash-equity and crypto
    # bucket. Callers check ``is_file()`` rather than assuming it exists.
    @property
    def contracts_yaml_path(self) -> Path:
        return self.folder / "contracts.yaml"

    def contracts_yaml_path_for(self, scanner: str = "") -> Path:
        return (
            self.folder / f"contracts_{scanner}.yaml"
            if scanner
            else self.contracts_yaml_path
        )

    def trades_derivatives(self, scanner: str = "") -> bool:
        """True when this bucket routes signals into derivative contracts."""
        return self.contracts_yaml_path_for(scanner).is_file()

    @property
    def strategy_master_csv_path(self) -> Path:
        return self.folder / "strategy_master.csv"

    @property
    def strategies_folder(self) -> Path:
        return self.folder / "strategies"


def _split_bucket_id(bucket_id: str) -> tuple[TradingType, Market]:
    try:
        type_str, market_str = bucket_id.split("-", maxsplit=1)
    except ValueError as e:  # pragma: no cover  — defensive
        raise ValueError(
            f"Invalid bucket id {bucket_id!r}; expected '<type>-<market>'"
        ) from e
    return TradingType(type_str), Market(market_str)


def load_buckets(
    buckets_yaml: Path | None = None,
    strategies_root: Path | None = None,
) -> list[Bucket]:
    """Load every bucket declared in ``buckets.yaml`` and locate its folder.

    A bucket folder ``src/strategies/<type>/<market>/`` must exist for every
    enabled entry. Disabled buckets may or may not have folders yet — they
    are returned but skipped by the runner.

    Raises:
        FileNotFoundError: if a required folder is missing.
        ValidationError: if ``buckets.yaml`` does not match the schema.
    """
    path = buckets_yaml or _BUCKETS_YAML_PATH
    strategies = strategies_root or (_REPO_ROOT / "src" / "strategies")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = BucketsConfig.model_validate(raw)

    out: list[Bucket] = []
    for bucket_id, bcfg in cfg.buckets.items():
        ttype, market = _split_bucket_id(bucket_id)
        folder = strategies / ttype.value / market.value
        if bcfg.enabled and not folder.is_dir():
            raise FileNotFoundError(
                f"Bucket {bucket_id!r} is enabled but folder is missing: {folder}"
            )
        out.append(
            Bucket(
                id=bucket_id,
                trading_type=ttype,
                market=market,
                config=bcfg,
                folder=folder,
            )
        )
    return out


def load_bucket(bucket_id: str, **kwargs: object) -> Bucket:
    """Convenience: load all and return the one matching ``bucket_id``.

    Raises ``KeyError`` if not found.
    """
    for b in load_buckets(**kwargs):  # type: ignore[arg-type]
        if b.id == bucket_id:
            return b
    raise KeyError(f"Bucket {bucket_id!r} not found in buckets.yaml")
