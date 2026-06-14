"""Strategy params views — three separate pages per bucket.

Replaces the legacy JSON-dump page with structured pages keyed by
concern:

    /params                          bucket index grid
    /params/allocation/<bucket_id>   Kelly + caps + FX + contract sizes
    /params/trading/<bucket_id>      Strategy Master + per-strategy detail
    /params/scanner/<bucket_id>      universe selection (filters + ranker)

The split makes it easy to find the knob you're looking for without
scrolling through a wall of JSON.
"""

from __future__ import annotations

import inspect

from fastapi import APIRouter, HTTPException, Request

from src.shared.allocator.sizer import load_allocator_config
from src.shared.bucket import Bucket, load_buckets
from src.shared.scanner.engine import load_scanner_config
from src.shared.strategy_loader import discover_strategies
from src.shared.strategy_master.loader import load_strategy_master

router = APIRouter(prefix="/params", tags=["params"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_bucket(bucket_id: str) -> Bucket:
    for b in load_buckets():
        if b.id == bucket_id:
            return b
    raise HTTPException(status_code=404, detail=f"bucket {bucket_id!r} not found")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
@router.get("")
def params_index(request: Request):
    """Grid of bucket cards linking to the three views."""
    cards = []
    for b in load_buckets():
        cards.append(
            {
                "id": b.id,
                "trading_type": b.trading_type.value,
                "market": b.market.value,
                "broker": b.config.broker.value,
                "enabled": b.config.enabled,
                "capital_inr": str(b.config.capital_inr),
                "leverage_max": str(b.config.leverage_max),
            }
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "params.html", {"cards": cards})


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
@router.get("/allocation/{bucket_id}")
def allocation_view(bucket_id: str, request: Request):
    bucket = _find_bucket(bucket_id)
    try:
        cfg = load_allocator_config(bucket.allocator_yaml_path)
        err = None
        cfg_dict = cfg.model_dump(mode="json")
    except FileNotFoundError:
        cfg_dict = None
        err = f"allocator.yaml missing for {bucket_id}"
    except Exception as e:
        cfg_dict = None
        err = f"parse error: {e}"

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "params_allocation.html",
        {
            "bucket": _bucket_summary(bucket),
            "cfg": cfg_dict,
            "error": err,
        },
    )


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------
@router.get("/trading/{bucket_id}")
def trading_view(bucket_id: str, request: Request):
    bucket = _find_bucket(bucket_id)

    master_rows = []
    master_err = None
    try:
        master = load_strategy_master(
            bucket.strategy_master_csv_path,
            bucket_trading_type=bucket.trading_type.value,
        )
        for r in master.rows:
            master_rows.append(
                {
                    "strategy_name": r.strategy_name,
                    "tf": r.tf,
                    "min_vol": str(r.min_vol) if r.min_vol is not None else "—",
                    "trading_regime_1": (
                        r.trading_regime_1.value if r.trading_regime_1 else "—"
                    ),
                    "trading_regime_2": (
                        r.trading_regime_2.value if r.trading_regime_2 else "—"
                    ),
                    "trading_type": r.trading_type,
                    "regime_gate": sorted(g.value for g in r.allowed_regimes)
                    or ["(any)"],
                }
            )
    except Exception as e:
        master_err = str(e)

    strategies = []
    discovered_err = None
    try:
        discovered = discover_strategies(bucket.strategies_folder)
        for name, cls in discovered.items():
            doc = inspect.getdoc(cls) or "(no docstring)"
            strategies.append(
                {
                    "name": name,
                    "tf": cls.tf,
                    "trading_type": cls.trading_type,
                    "docstring": doc,
                    "filename": f"{name}.py",
                }
            )
    except Exception as e:
        discovered_err = str(e)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "params_trading.html",
        {
            "bucket": _bucket_summary(bucket),
            "master_rows": master_rows,
            "master_err": master_err,
            "strategies": strategies,
            "discovered_err": discovered_err,
        },
    )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
@router.get("/scanner/{bucket_id}")
def scanner_view(bucket_id: str, request: Request):
    bucket = _find_bucket(bucket_id)
    cfg_dict = None
    err = None
    try:
        cfg = load_scanner_config(bucket.scanner_yaml_path)
        cfg_dict = {
            "universe_size": cfg.universe_size,
            "filters": [
                {"name": f.name, "params": f.params} for f in cfg.filters
            ],
            "ranker": {"name": cfg.ranker.name, "params": cfg.ranker.params},
        }
    except FileNotFoundError:
        err = "scanner.yaml missing"
    except Exception as e:
        err = f"parse error: {e}"

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "params_scanner.html",
        {
            "bucket": _bucket_summary(bucket),
            "cfg": cfg_dict,
            "error": err,
        },
    )


# ---------------------------------------------------------------------------
# Shared bucket header
# ---------------------------------------------------------------------------
def _bucket_summary(bucket: Bucket) -> dict[str, object]:
    return {
        "id": bucket.id,
        "trading_type": bucket.trading_type.value,
        "market": bucket.market.value,
        "broker": bucket.config.broker.value,
        "enabled": bucket.config.enabled,
        "capital_inr": str(bucket.config.capital_inr),
        "leverage_max": str(bucket.config.leverage_max),
    }
