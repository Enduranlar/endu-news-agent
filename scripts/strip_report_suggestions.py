#!/usr/bin/env python3
"""Remove the source-suggestions section from already-archived reports.

Reports no longer include the "Önerilen yeni kaynaklar (onayınızı bekliyor)"
section (suggestions are reviewed in the web admin UI). This strips that section
from reports generated before the change. It removes the section heading and its
body up to the next heading / end of file; everything else is left untouched. No
API key needed — it's a plain text edit.

Reports are read from settings.REPORTS_DIR (honours AGENT_STATE_DIR). Since that's
version-controlled, review the diff and commit with scripts/sync_state.py after.

Usage:
  python scripts/strip_report_suggestions.py --dry-run    # preview
  python scripts/strip_report_suggestions.py              # rewrite in place
  python scripts/strip_report_suggestions.py 2026/2026-07-01_friday.md  # a file
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly: `python scripts/strip_report_suggestions.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings  # noqa: E402

# Heading text used by the suggestions section (Turkish now, English in very old
# reports). Match on a substring so minor wording differences still hit.
_HEADINGS = ("Önerilen yeni kaynaklar", "Suggested new sources")


def _is_suggestions_heading(line: str) -> bool:
    return line.startswith("## ") and any(h in line for h in _HEADINGS)


def strip_suggestions(text: str) -> tuple[str, bool]:
    """Return (new_text, changed). Removes the suggestions section if present."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        if _is_suggestions_heading(line):
            changed = True
            i += 1
            # Skip the section body until the next level-1/2 heading or EOF.
            while i < len(lines) and not lines[i].startswith(("## ", "# ")):
                i += 1
            continue
        out.append(line)
        i += 1

    if not changed:
        return text, False

    # Trim trailing blank lines (the separator before the removed section), then
    # restore a single trailing newline if the original had one.
    while out and not out[-1].strip():
        out.pop()
    new_text = "\n".join(out)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, True


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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Strip the suggestions section from reports.")
    ap.add_argument("paths", nargs="*", help="specific report files (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    args = ap.parse_args(argv)

    targets = _resolve_targets(args.paths)
    if not targets:
        print(f"No reports found under {settings.REPORTS_DIR}.")
        return 0

    changed = 0
    for path in targets:
        text = path.read_text(encoding="utf-8")
        new_text, did = strip_suggestions(text)
        rel = str(path.relative_to(settings.REPORTS_DIR))
        if did:
            changed += 1
            print(f"{'[dry-run] ' if args.dry_run else ''}{rel}: suggestions section removed")
            if not args.dry_run:
                path.write_text(new_text, encoding="utf-8")
        else:
            print(f"{rel}: no suggestions section")

    print(
        f"\n{'[dry-run] would update' if args.dry_run else 'Updated'} "
        f"{changed} of {len(targets)} report(s)."
    )
    if not args.dry_run and changed and settings.STATE_DIR != settings.ROOT:
        print("Commit the change: python scripts/sync_state.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
