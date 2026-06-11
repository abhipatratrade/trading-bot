"""Smoke tests: every service entrypoint must be importable.

These tests caught nothing yet but are scoped to catch the class of bug
we hit on 2026-06-12:

- ``run_scheduler.py`` imported ``retrain_job`` which imported a
  non-existent ``BinancePublicData`` class → ImportError → Railway
  scheduler crashlooped.
- ``hmm_model.py`` top-level imported ``hmmlearn`` → ImportError on
  Railway where the build couldn't compile hmmlearn → scheduler
  crashlooped.

If an entrypoint module can be imported without raising, the worst that
happens is a runtime error on the specific path that needs the missing
dependency. The service stays up.

We do NOT instantiate or run anything here — that's deliberate. Imports
only.
"""

from __future__ import annotations

import importlib


def test_run_bot_imports() -> None:
    importlib.import_module("src.entrypoints.run_bot")


def test_run_dashboard_imports() -> None:
    importlib.import_module("src.entrypoints.run_dashboard")


def test_run_scheduler_imports() -> None:
    importlib.import_module("src.entrypoints.run_scheduler")


def test_dashboard_app_imports() -> None:
    importlib.import_module("src.dashboard.app")


def test_regime_modules_import_without_hmmlearn() -> None:
    """Importing the brain entrypoints must not require hmmlearn at import time.

    Production environments (Railway nixpacks) sometimes fail to install
    hmmlearn. The scheduler / dashboard / dashboard routes all transit
    through these modules and must boot cleanly even then. Actual
    ``RegimeModel.fit`` / ``from_dict`` calls then fail loudly at runtime
    with a clear ``ImportError`` from ``_import_gaussian_hmm``.
    """
    importlib.import_module("src.shared.regime.brain")
    importlib.import_module("src.shared.regime.hmm_model")
    importlib.import_module("src.shared.regime.store")
    importlib.import_module("src.shared.regime.retrain_job")


def test_bucket_runner_imports() -> None:
    importlib.import_module("src.shared.bucket_runner")
