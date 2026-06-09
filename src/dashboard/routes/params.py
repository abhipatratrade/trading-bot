"""Strategy params snapshot page — walks bucket configs.

Replaces the legacy ``*/policy.yaml`` lookup with the new per-bucket layout:
each bucket has ``scanner.yaml`` + ``regime.yaml`` + ``allocator.yaml`` +
``strategy_master.csv``. We render them grouped by bucket for review.
"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, Request

from src.shared.bucket import load_buckets

router = APIRouter(prefix="/params", tags=["params"])


@router.get("")
def params_page(request: Request):
    snapshots = _load_all_bucket_configs()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "params.html",
        {"strategies": snapshots},
    )


def _load_all_bucket_configs() -> list[dict]:
    results: list[dict] = []
    for bucket in load_buckets():
        bundle: dict[str, object] = {"bucket": bucket.id, "enabled": bucket.config.enabled}
        for name, path in (
            ("scanner", bucket.scanner_yaml_path),
            ("regime", bucket.regime_yaml_path),
            ("allocator", bucket.allocator_yaml_path),
        ):
            try:
                bundle[name] = yaml.safe_load(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                bundle[name] = {"_error": f"missing {path.name}"}
            except Exception as e:
                bundle[name] = {"_error": str(e)}
        try:
            bundle["strategy_master"] = bucket.strategy_master_csv_path.read_text(
                encoding="utf-8"
            )
        except FileNotFoundError:
            bundle["strategy_master"] = ""
        results.append({"name": bucket.id, "policy": bundle})
    return results
