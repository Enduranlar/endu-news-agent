#!/usr/bin/env python3
"""Retroactively de-duplicate already-archived reports.

The report-build pipeline de-duplicates repeated stories going forward, but reports
generated before that are already rendered Markdown in the archive. This script
reads each archived report, runs the SAME report-wide dedup across its category item
bullets (`- **[n/5]** … ([source](url))`), removes duplicates keeping the best per
cluster, rewrites the file, drops any category section left empty, and updates
reports/index.json + index.md counts.

Only the category item bullets are touched — the intro, "Yaklaşan Türkiye
yarışları", and "Önerilen yeni kaynaklar" sections are left as-is.

Reports are read from settings.REPORTS_DIR (honours AGENT_STATE_DIR). Since that's
version-controlled, review the diff / commit with scripts/sync_state.py afterwards.

Usage:
  python scripts/dedupe_reports.py                 # all archived reports
  python scripts/dedupe_reports.py --dry-run       # show what would change
  python scripts/dedupe_reports.py 2026/2026-07-01_friday.md   # specific file(s)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Allow running directly: `python scripts/dedupe_reports.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src import archive, settings  # noqa: E402
from src.llm import LLMClient  # noqa: E402
from src.report import duplicate_drop_indices  # noqa: E402

_ITEM_RE = re.compile(r"^- \*\*\[(\d)/5\]\*\* (.*)$")
_H2_RE = re.compile(r"^## (.*)$")


def _extract_one_line(rest: str) -> str:
    """Strip the trailing ' ([source](url)) — date' from a rendered bullet."""
    return re.split(r"\s*\(\[", rest, maxsplit=1)[0].strip() or rest


def dedupe_markdown(text: str, llm: LLMClient) -> tuple[str, int]:
    """Return (new_markdown, removed_count) for one report's Markdown."""
    lines = text.splitlines()

    section = ""
    entries: list[dict] = []  # {line, importance, category, one_line}
    for idx, line in enumerate(lines):
        h = _H2_RE.match(line)
        if h:
            section = h.group(1).strip()
            continue
        m = _ITEM_RE.match(line)
        if m:
            entries.append(
                {
                    "line": idx,
                    "importance": int(m.group(1)),
                    "category": section,
                    "one_line": _extract_one_line(m.group(2)),
                }
            )

    if len(entries) < 2:
        return text, 0

    drop_idx = duplicate_drop_indices(entries, llm)
    if not drop_idx:
        return text, 0
    drop_lines = {entries[i]["line"] for i in drop_idx}
    kept = [ln for j, ln in enumerate(lines) if j not in drop_lines]

    # Drop any level-2 section whose body is now entirely blank.
    out: list[str] = []
    i = 0
    while i < len(kept):
        line = kept[i]
        if line.startswith("## "):
            j = i + 1
            while j < len(kept) and not kept[j].startswith(("## ", "# ")):
                j += 1
            body = kept[i + 1 : j]
            if any(b.strip() for b in body):
                out.append(line)
                out.extend(body)
            i = j
        else:
            out.append(line)
            i += 1

    new_text = "\n".join(out)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, len(drop_lines)


def _count_items(text: str) -> int:
    return sum(1 for ln in text.splitlines() if _ITEM_RE.match(ln))


def _resolve_targets(args_paths: list[str]) -> list[Path]:
    base = settings.REPORTS_DIR
    if args_paths:
        out = []
        for p in args_paths:
            path = Path(p)
            if not path.is_absolute():
                path = base / p
            if path.exists():
                out.append(path)
            else:
                print(f"skip (not found): {p}", file=sys.stderr)
        return out
    return sorted(base.glob("*/*.md"))


def _update_index_counts(processed: dict[str, int]) -> None:
    """Update item_count in reports/index.json for reprocessed files, then
    regenerate index.md. `processed` maps relpath -> new item count."""
    import json

    ipath = archive._index_json_path()
    if not ipath.exists():
        return
    try:
        entries = json.loads(ipath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    changed = False
    for e in entries:
        if e.get("path") in processed:
            e["item_count"] = processed[e["path"]]
            changed = True
    if changed:
        ipath.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        archive._update_index_md()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="De-duplicate archived reports.")
    ap.add_argument("paths", nargs="*", help="specific report files (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    args = ap.parse_args(argv)

    load_dotenv(settings.ROOT / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is not set (needed for dedup).", file=sys.stderr)
        return 2
    llm = LLMClient(
        api_key,
        os.environ.get("LLM_FILTER_MODEL", "claude-haiku-4-5").strip()
        or "claude-haiku-4-5",
        os.environ.get("LLM_SUMMARY_MODEL", "claude-sonnet-4-6").strip()
        or "claude-sonnet-4-6",
    )

    targets = _resolve_targets(args.paths)
    if not targets:
        print(f"No reports found under {settings.REPORTS_DIR}.")
        return 0

    total_removed = 0
    processed_counts: dict[str, int] = {}
    for path in targets:
        text = path.read_text(encoding="utf-8")
        new_text, removed = dedupe_markdown(text, llm)
        rel = str(path.relative_to(settings.REPORTS_DIR))
        if removed:
            total_removed += removed
            print(f"{'[dry-run] ' if args.dry_run else ''}{rel}: -{removed} duplicate(s)")
            if not args.dry_run:
                path.write_text(new_text, encoding="utf-8")
                processed_counts[rel] = _count_items(new_text)
        else:
            print(f"{rel}: no duplicates")

    if processed_counts and not args.dry_run:
        _update_index_counts(processed_counts)

    print(
        f"\n{'[dry-run] would remove' if args.dry_run else 'Removed'} "
        f"{total_removed} duplicate line(s) across {len(targets)} report(s)."
    )
    if not args.dry_run and total_removed and settings.STATE_DIR != settings.ROOT:
        print("Commit the change: python scripts/sync_state.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
