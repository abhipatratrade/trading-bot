"""Strategy params snapshot page."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, Request

router = APIRouter(prefix="/params", tags=["params"])

_STRATEGIES_DIR = Path(__file__).parents[2] / "strategies"


@router.get("")
def params_page(request: Request):
    snapshots = _load_all_policies()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "params.html",
        {"request": request, "strategies": snapshots},
    )


def _load_all_policies() -> list[dict]:
    """Walk strategy directories and load each policy.yaml."""
    results: list[dict] = []
    if not _STRATEGIES_DIR.exists():
        return results
    for policy_path in sorted(_STRATEGIES_DIR.glob("*/policy.yaml")):
        strategy_name = policy_path.parent.name
        try:
            with open(policy_path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {"_error": f"Could not parse {policy_path}"}
        results.append({"name": strategy_name, "policy": data})
    return results
