"""
Materialise stored session reports into ``docs/journal/YYYY-MM-DD.md`` for git.

The reports live in Postgres because the Railway scheduler container is
ephemeral and holds no git credentials. This script is the bridge: run it where
the repo is checked out (the VM, or locally) to write the markdown files, then
commit them.

    python -m scripts.export_journal                 # every report not yet on disk
    python -m scripts.export_journal --date 2026-07-28
    python -m scripts.export_journal --all           # rewrite existing files too
    python -m scripts.export_journal --commit        # also git add + commit

``--commit`` is deliberately opt-in. It is safe on the VM — ops/deploy.sh's
RESTART_PATHS excludes docs/, so a journal commit does not restart the bot —
but pushing is left to you.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import date
from pathlib import Path

from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import SessionReport

JOURNAL_DIR = Path(__file__).resolve().parents[1] / "docs" / "journal"


def export(
    *, only: date | None = None, overwrite: bool = False
) -> list[Path]:
    """Write reports to docs/journal/. Returns the paths actually written."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with session_scope() as session:
        stmt = select(SessionReport).order_by(SessionReport.session_date)
        if only is not None:
            stmt = stmt.where(SessionReport.session_date == only)
        for row in session.execute(stmt).scalars():
            path = JOURNAL_DIR / f"{row.session_date.isoformat()}.md"
            if path.exists() and not overwrite:
                continue
            path.write_text(row.markdown, encoding="utf-8")
            written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--all", action="store_true", help="Rewrite files that already exist."
    )
    parser.add_argument(
        "--commit", action="store_true", help="git add + commit the written files."
    )
    args = parser.parse_args()

    written = export(only=args.date, overwrite=args.all)
    if not written:
        print("Nothing to write — every stored report is already on disk.")
        return 0

    for path in written:
        print(f"wrote {path}")

    if args.commit:
        # S603/S607: `git` resolved from PATH with a fixed argv. The only
        # interpolated values are paths this script just wrote into
        # JOURNAL_DIR, and argv is a list, so there is no shell to inject into.
        add = ["git", "add", *[str(p) for p in written]]
        commit = [
            "git",
            "commit",
            "-m",
            f"docs(journal): add {len(written)} session report(s)",
        ]
        subprocess.run(add, check=True)  # noqa: S603, S607
        subprocess.run(commit, check=True)  # noqa: S603, S607
        print("committed (not pushed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
