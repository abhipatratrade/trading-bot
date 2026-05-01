"""Launch the dashboard with uvicorn."""

from __future__ import annotations

import uvicorn

from src.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "src.dashboard.app:create_app",
        factory=True,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
