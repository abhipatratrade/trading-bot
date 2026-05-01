"""Launch the dashboard with uvicorn."""

from __future__ import annotations

import os

import uvicorn

from src.core.config import get_settings


def main() -> None:
    settings = get_settings()
    port = int(os.environ.get("PORT", settings.dashboard_port))
    uvicorn.run(
        "src.dashboard.app:create_app",
        factory=True,
        host=settings.dashboard_host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
