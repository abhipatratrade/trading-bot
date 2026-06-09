"""
Dynamic discovery of ``Strategy`` subclasses in a bucket's ``strategies/`` folder.

The dashboard's "Start New Trade" button (per PPTX slide 2/3) triggers a
fresh scan of each bucket's strategy folder. New strategy files are picked
up without a deploy.

Discovery rules:
- Only ``*.py`` files at the top level of ``strategies/`` are considered.
- Files starting with ``_`` are skipped.
- Each module must expose exactly one ``Strategy`` subclass.
- The subclass's ``name`` ClassVar must equal the module filename
  (e.g. ``top5_volume`` in ``top5_volume.py``). Fail-fast otherwise.

The loader does NOT import any strategy proactively at module import
time — only when called. This keeps strategies importable by the
backtester without spinning up the live trading stack.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

from src.core.logging import get_logger
from src.shared.base_strategy import Strategy

_log = get_logger("shared.strategy_loader")


class StrategyLoadError(Exception):
    """Raised when a strategy file fails validation rules."""


def discover_strategies(strategies_folder: Path) -> dict[str, type[Strategy]]:
    """Return ``{strategy_name: StrategyClass}`` for the given folder.

    Skips silently if the folder does not exist (empty bucket).
    """
    if not strategies_folder.is_dir():
        return {}

    out: dict[str, type[Strategy]] = {}
    for py in sorted(strategies_folder.glob("*.py")):
        if py.name.startswith("_"):
            continue
        name = py.stem
        cls = _load_one(py, expected_name=name)
        out[name] = cls
    _log.debug(
        "strategies_discovered",
        folder=str(strategies_folder),
        names=list(out.keys()),
    )
    return out


def _load_one(path: Path, expected_name: str) -> type[Strategy]:
    """Import a single strategy file and return its Strategy subclass."""
    module_name = f"_loaded_strategy_{path.stem}_{id(path)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise StrategyLoadError(f"Cannot build import spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover  — re-raised with context
        raise StrategyLoadError(f"Import failed for {path}: {e}") from e

    subclasses = [
        cls
        for _, cls in inspect.getmembers(mod, inspect.isclass)
        if issubclass(cls, Strategy) and cls is not Strategy and cls.__module__ == module_name
    ]
    if len(subclasses) != 1:
        raise StrategyLoadError(
            f"{path}: expected exactly one Strategy subclass, found {len(subclasses)}"
        )

    cls = subclasses[0]
    if cls.name != expected_name:
        raise StrategyLoadError(
            f"{path}: Strategy.name={cls.name!r} does not match filename "
            f"{expected_name!r}"
        )
    return cls
