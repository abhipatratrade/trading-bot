"""
Symbol mapping — fetch perpetual symbols from Binance + Delta, find the
intersection, and persist to CSV or the ``symbol_mapping`` DB table.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from src.core.logging import get_logger
from src.data_sources.binance import BinanceData
from src.data_sources.delta_india import DeltaIndiaData

log = get_logger("data_sources.symbol_loader")

DEFAULT_CSV = Path(__file__).resolve().parent.parent.parent / "data" / "symbol_mapping.csv"

_CSV_COLUMNS = [
    "canonical_symbol",
    "binance_symbol",
    "delta_symbol",
    "listed_on_binance",
    "listed_on_delta",
]


def fetch_mappings(
    binance: BinanceData | None = None,
    delta: DeltaIndiaData | None = None,
) -> list[dict[str, Any]]:
    """Fetch perpetual symbol lists from both exchanges, return merged rows.

    Each row is a dict with keys matching :data:`_CSV_COLUMNS`.
    """
    own_binance = binance is None
    own_delta = delta is None
    if own_binance:
        binance = BinanceData()
    if own_delta:
        delta = DeltaIndiaData()

    try:
        # Binance: {baseAsset -> USDT perp symbol}
        # Sort so USDT pairs are processed last and win over USDC/BUSD.
        binance_perps = binance.get_perpetual_symbols()  # type: ignore[union-attr]
        binance_map: dict[str, str] = {}
        for p in sorted(binance_perps, key=lambda x: x["symbol"].endswith("USDT")):
            binance_map[p["baseAsset"]] = p["symbol"]

        # Delta: {baseAsset -> Delta symbol}
        delta_perps = delta.get_perpetual_symbols()  # type: ignore[union-attr]
        delta_map: dict[str, str] = {
            p["baseAsset"]: p["symbol"] for p in delta_perps
        }
    finally:
        if own_binance:
            binance.close()  # type: ignore[union-attr]
        if own_delta:
            delta.close()  # type: ignore[union-attr]

    # Union of all base assets
    all_bases = sorted(set(binance_map) | set(delta_map))

    rows: list[dict[str, Any]] = []
    for base in all_bases:
        rows.append(
            {
                "canonical_symbol": base,
                "binance_symbol": binance_map.get(base, ""),
                "delta_symbol": delta_map.get(base, ""),
                "listed_on_binance": base in binance_map,
                "listed_on_delta": base in delta_map,
            }
        )

    both = sum(1 for r in rows if r["listed_on_binance"] and r["listed_on_delta"])
    log.info(
        "mappings_fetched",
        binance_count=len(binance_map),
        delta_count=len(delta_map),
        overlap=both,
        total=len(rows),
    )
    return rows


def save_csv(rows: list[dict[str, Any]], path: Path = DEFAULT_CSV) -> Path:
    """Write mappings to CSV.  Creates parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("csv_saved", path=str(path), rows=len(rows))
    return path


def load_csv(path: Path = DEFAULT_CSV) -> list[dict[str, Any]]:
    """Read mappings back from CSV."""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    for r in rows:
        r["listed_on_binance"] = r["listed_on_binance"] == "True"
        r["listed_on_delta"] = r["listed_on_delta"] == "True"
    return rows


def load_to_db(rows: list[dict[str, Any]]) -> int:
    """Upsert rows into the ``symbol_mapping`` table.  Returns count."""
    from sqlalchemy import select

    from src.core.db import session_scope
    from src.core.models import SymbolMapping

    count = 0
    with session_scope() as session:
        for r in rows:
            canonical = r["canonical_symbol"]
            existing = session.execute(
                select(SymbolMapping).where(
                    SymbolMapping.canonical_symbol == canonical
                )
            ).scalar_one_or_none()

            if existing:
                existing.binance_symbol = r["binance_symbol"] or None
                existing.delta_symbol = r["delta_symbol"] or None
                existing.listed_on_binance = r["listed_on_binance"]
                existing.listed_on_delta = r["listed_on_delta"]
            else:
                session.add(
                    SymbolMapping(
                        canonical_symbol=canonical,
                        binance_symbol=r["binance_symbol"] or None,
                        delta_symbol=r["delta_symbol"] or None,
                        listed_on_binance=r["listed_on_binance"],
                        listed_on_delta=r["listed_on_delta"],
                        listed_on_kite=False,
                    )
                )
            count += 1
    log.info("db_upserted", count=count)
    return count
