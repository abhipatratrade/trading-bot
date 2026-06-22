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
