"""
Strategy Master CSV loader.

Reads the CSV, validates every row against the schema, and ensures the
``trading_type`` column matches the bucket. Returns a list of validated
rows plus a name→row lookup. Fail-fast on schema or consistency errors
(Decision 006).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from src.shared.strategy_master.schema import StrategyMasterRow


class StrategyMasterError(Exception):
    """Raised when the CSV cannot be loaded or violates a constraint."""


@dataclass(frozen=True, slots=True)
class StrategyMaster:
    rows: list[StrategyMasterRow]
    by_name: dict[str, StrategyMasterRow]


def load_strategy_master(
    csv_path: Path, bucket_trading_type: str, bucket_tf: str | None = None
) -> StrategyMaster:
    """Load and validate a strategy_master.csv for a single bucket.

    Args:
        csv_path: path to ``strategy_master.csv``.
        bucket_trading_type: e.g. "longterm" — every row must match.
        bucket_tf: if set, every row's ``tf`` must equal this. (The bucket's
            regime/scanner TF is shared by all strategies in the bucket.)

    Raises:
        StrategyMasterError on any schema / consistency violation.
    """
    if not csv_path.is_file():
        raise StrategyMasterError(f"strategy_master.csv missing: {csv_path}")

    rows: list[StrategyMasterRow] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise StrategyMasterError(f"{csv_path}: empty CSV (no header)")

        required = {
            "strategy_name",
            "tf",
            "min_vol",
            "trading_regime_1",
            "trading_regime_2",
            "trading_type",
        }
        missing = required - set(reader.fieldnames)
        if missing:
            raise StrategyMasterError(
                f"{csv_path}: missing columns: {sorted(missing)}"
            )

        for line_no, raw in enumerate(reader, start=2):  # header is line 1
            try:
                row = StrategyMasterRow.model_validate(raw)
            except ValidationError as e:
                raise StrategyMasterError(
                    f"{csv_path}:{line_no}: {e.errors(include_url=False)}"
                ) from e

            if row.trading_type != bucket_trading_type:
                raise StrategyMasterError(
                    f"{csv_path}:{line_no}: trading_type={row.trading_type!r} "
                    f"does not match bucket type {bucket_trading_type!r}"
                )
            if bucket_tf is not None and row.tf != bucket_tf:
                raise StrategyMasterError(
                    f"{csv_path}:{line_no}: tf={row.tf!r} does not match "
                    f"bucket tf {bucket_tf!r}"
                )
            rows.append(row)

    by_name: dict[str, StrategyMasterRow] = {}
    for r in rows:
        if r.strategy_name in by_name:
            raise StrategyMasterError(
                f"{csv_path}: duplicate strategy_name {r.strategy_name!r}"
            )
        by_name[r.strategy_name] = r

    return StrategyMaster(rows=rows, by_name=by_name)
