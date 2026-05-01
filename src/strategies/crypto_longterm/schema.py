"""
Schema validation for crypto_longterm policy.yaml.

Fail-fast: invalid YAML refuses to start the bot (Decision 006).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

_POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"


class WeightMode(StrEnum):
    EQUAL = "equal"


class CryptoLongtermPolicy(BaseModel):
    version: int = Field(ge=1)
    backtest_ref: str = Field(min_length=1)

    strategy_id: Literal["crypto_longterm"]
    timeframe: Literal["1d"]
    broker: Literal["delta_india"]

    max_positions: int = Field(ge=1, le=20)
    volume_rank_source: Literal["delta_india"]
    min_24h_volume_usd: Decimal = Field(ge=0)

    leverage: int = Field(ge=1, le=20)
    weight_mode: WeightMode

    rebalance_hour: int = Field(ge=0, le=23)
    rebalance_minute: int = Field(ge=0, le=59)

    max_daily_drawdown_pct: Decimal = Field(ge=Decimal("0.1"), le=Decimal("50"))
    min_liquidation_distance_pct: Decimal = Field(ge=Decimal("1"), le=Decimal("50"))
    max_funding_rate: Decimal = Field(ge=Decimal("0.0001"), le=Decimal("0.1"))

    order_type: Literal["market", "limit"]

    @field_validator("min_24h_volume_usd", mode="before")
    @classmethod
    def _parse_volume(cls, v: object) -> object:
        if isinstance(v, str):
            return Decimal(v.replace("_", ""))
        return v


def load_policy(path: Path | None = None) -> CryptoLongtermPolicy:
    """Load and validate policy.yaml. Raises on any error."""
    p = path or _POLICY_PATH
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return CryptoLongtermPolicy.model_validate(raw)
