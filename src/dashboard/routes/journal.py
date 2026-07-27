"""Session journal — the stored end-of-day postmortems (Decision 033, Tier 3).

Read-only. The reports are built by the scheduler's ``eod_report`` job and
stored in ``session_report``; this just renders them newest-first with a date
picker. Markdown is converted with a tiny local renderer rather than a
dependency — the generator emits a known, closed subset (headings, tables,
lists, bold, code spans), so a full CommonMark parser would be dead weight.
"""

from __future__ import annotations

import html
import re
from datetime import date as date_

from fastapi import APIRouter, Request
from sqlalchemy import select

from src.core.db import session_scope
from src.core.models import SessionReport

router = APIRouter()

_INLINE = (
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"`(.+?)`"), r"<code>\1</code>"),
    (re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)"), r"<em>\1</em>"),
)


def _inline(text: str) -> str:
    """Escape first, then apply the inline subset — never the other way round."""
    out = html.escape(text)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def _row_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render_markdown_to_html(markdown: str) -> str:
    """Render the closed subset ``eod.render_markdown`` emits. PURE.

    Everything is HTML-escaped before any tag is introduced, so a symbol or
    broker message containing angle brackets cannot inject markup.
    """
    out: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append(
                f"<h{level}>{_inline(stripped[level:].strip())}</h{level}>"
            )
            i += 1
            continue

        # A table is a header row, a separator row, then body rows.
        if (
            stripped.startswith("|")
            and i + 1 < len(lines)
            and set(lines[i + 1].strip()) <= set("|-: ")
            and "-" in lines[i + 1]
        ):
            head = _row_cells(stripped)
            out.append("<table><thead><tr>")
            out.extend(f"<th>{_inline(c)}</th>" for c in head)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                out.append("<tr>")
                out.extend(f"<td>{_inline(c)}</td>" for c in _row_cells(lines[i]))
                out.append("</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        if stripped.startswith("- "):
            out.append("<ul>")
            while i < len(lines) and lines[i].strip().startswith("- "):
                out.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("</ul>")
            continue

        out.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    return "\n".join(out)


@router.get("/journal")
def journal_index(request: Request, d: str | None = None):
    """Newest report by default; ``?d=YYYY-MM-DD`` picks a specific session."""
    wanted: date_ | None = None
    if d:
        try:
            wanted = date_.fromisoformat(d)
        except ValueError:
            wanted = None

    with session_scope() as session:
        dates = list(
            session.execute(
                select(SessionReport.session_date)
                .order_by(SessionReport.session_date.desc())
                .limit(120)
            ).scalars()
        )
        stmt = select(SessionReport)
        stmt = (
            stmt.where(SessionReport.session_date == wanted)
            if wanted is not None
            else stmt.order_by(SessionReport.session_date.desc())
        )
        row = session.execute(stmt).scalars().first()
        report = (
            {
                "date": row.session_date.isoformat(),
                "html": render_markdown_to_html(row.markdown),
            }
            if row is not None
            else None
        )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "report": report,
            "dates": [x.isoformat() for x in dates],
            "selected": report["date"] if report else None,
        },
    )
