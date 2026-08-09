"""Write report files to the archive and maintain a searchable index.

Layout:
  reports/<YYYY>/<YYYY-MM-DD>_<period>.md   — the rendered Markdown report
  reports/index.json                         — append-only machine index
  reports/index.md                           — reverse-chronological human index
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from . import settings
from .report import ReportBundle
from .timeutil import istanbul_now

log = logging.getLogger("agent.archive")


@dataclass
class ArchivedReport:
    path: Path
    relpath: str


def archive_report(
    bundle: ReportBundle, reports_dir: Path | None = None
) -> ArchivedReport:
    """Archive a report. `reports_dir` defaults to the shared reports/ root; each
    agent passes its own reports/<agent>/ so fleets keep separate histories."""
    base = reports_dir or settings.REPORTS_DIR
    base.mkdir(parents=True, exist_ok=True)
    now = istanbul_now()
    year = now.strftime("%Y")
    day = now.strftime("%Y-%m-%d")
    year_dir = base / year
    year_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{day}_{bundle.period}.md"
    path = year_dir / filename
    path.write_text(bundle.markdown, encoding="utf-8")
    relpath = f"{year}/{filename}"

    _update_index_json(bundle, day, relpath, base)
    _update_index_md(base)
    log.info("archived report -> %s", path)
    return ArchivedReport(path=path, relpath=relpath)


def _index_json_path(base: Path | None = None) -> Path:
    return (base or settings.REPORTS_DIR) / "index.json"


def _update_index_json(bundle: ReportBundle, day: str, relpath: str,
                       base: Path | None = None) -> None:
    path = _index_json_path(base)
    entries = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append(
        {
            "date": day,
            "period": bundle.period,
            "path": relpath,
            "item_count": bundle.item_count,
            "categories_covered": bundle.categories_covered,
            "generated_at": istanbul_now().isoformat(),
        }
    )
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def _update_index_md(base: Path | None = None) -> None:
    """Regenerate the human-readable index from index.json, newest first."""
    base = base or settings.REPORTS_DIR
    entries = []
    jpath = _index_json_path(base)
    if jpath.exists():
        try:
            entries = json.loads(jpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []

    entries_sorted = sorted(
        entries,
        key=lambda e: (e.get("date", ""), e.get("generated_at", "")),
        reverse=True,
    )
    lines = ["# Report archive index", ""]
    for e in entries_sorted:
        cats = ", ".join(e.get("categories_covered", []))
        lines.append(
            f"- **{e.get('date','')}** ({e.get('period','')}) — "
            f"[{e.get('path','')}]({e.get('path','')}) — "
            f"{e.get('item_count', 0)} items"
            + (f" — {cats}" if cats else "")
        )
    (base / "index.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
