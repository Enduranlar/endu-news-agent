#!/usr/bin/env python3
"""Reset local state: erase the database, logs, and archived reports.

⚠️  DESTRUCTIVE. This permanently deletes:
      - data/   : the SQLite database (state, dedup, races, suggestions, credits)
      - logs/   : agent.log (+ rotations) and cron.log
      - reports/: archived report files and index.md / index.json

It does NOT touch your config (igaccounts.md, websites.md, interests.yaml) or .env.

By default it lists what it will delete and asks you to confirm. After a reset the
next `daily` run starts fresh — every source is treated as a first run, so the
INGEST_SINCE_DATE floor (or the first-run limits) applies again.

Usage:
  python scripts/reset.py                 # erase db + logs + reports (asks to confirm)
  python scripts/reset.py --yes           # skip the confirmation prompt
  python scripts/reset.py --dry-run       # show what would be deleted, delete nothing
  python scripts/reset.py --db            # only the database
  python scripts/reset.py --reports --logs
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# Allow running directly: `python scripts/reset.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings  # noqa: E402  (after sys.path tweak)


def collect_db() -> list[Path]:
    """Database file + any SQLite sidecars (WAL/SHM/journal)."""
    if not settings.DATA_DIR.exists():
        return []
    out = []
    for p in sorted(settings.DATA_DIR.iterdir()):
        if p.is_file() and (
            p.suffix == ".db"
            or p.name.endswith((".db-wal", ".db-shm", ".db-journal"))
        ):
            out.append(p)
    return out


def collect_logs() -> list[Path]:
    """agent.log, rotated agent.log.N, and cron.log."""
    if not settings.LOGS_DIR.exists():
        return []
    out = []
    for p in sorted(settings.LOGS_DIR.iterdir()):
        if p.is_file() and (p.suffix == ".log" or re.search(r"\.log\.\d+$", p.name)):
            out.append(p)
    return out


def collect_reports() -> list[Path]:
    """index.md / index.json and the per-year archive directories."""
    rd = settings.REPORTS_DIR
    if not rd.exists():
        return []
    out = []
    for name in ("index.md", "index.json"):
        p = rd / name
        if p.exists():
            out.append(p)
    for p in sorted(rd.iterdir()):
        if p.is_dir() and re.fullmatch(r"\d{4}", p.name):  # e.g. reports/2026/
            out.append(p)
    return out


def human(p: Path) -> str:
    try:
        rel = p.relative_to(settings.ROOT)
    except ValueError:
        rel = p
    return f"{rel}{'/' if p.is_dir() else ''}"


def delete(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Erase local database, logs, and archived reports."
    )
    parser.add_argument("--db", action="store_true", help="delete the database")
    parser.add_argument("--logs", action="store_true", help="delete logs")
    parser.add_argument("--reports", action="store_true", help="delete archived reports")
    parser.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    parser.add_argument(
        "--dry-run", action="store_true", help="list targets, delete nothing"
    )
    args = parser.parse_args(argv)

    # No specific target → everything.
    do_all = not (args.db or args.logs or args.reports)
    targets: list[Path] = []
    if do_all or args.db:
        targets += collect_db()
    if do_all or args.logs:
        targets += collect_logs()
    if do_all or args.reports:
        targets += collect_reports()

    if not targets:
        print("Nothing to delete — already clean.")
        return 0

    print("The following will be PERMANENTLY deleted:\n")
    for p in targets:
        print(f"  - {human(p)}")
    print()

    if args.dry_run:
        print(f"[dry-run] {len(targets)} item(s) would be deleted. Nothing changed.")
        return 0

    if not args.yes:
        answer = input(f"Delete these {len(targets)} item(s)? Type 'yes' to confirm: ")
        if answer.strip().lower() != "yes":
            print("Aborted. Nothing deleted.")
            return 1

    deleted = 0
    for p in targets:
        try:
            delete(p)
            deleted += 1
        except OSError as exc:
            print(f"  ! failed to delete {human(p)}: {exc}", file=sys.stderr)

    print(f"\nDeleted {deleted}/{len(targets)} item(s). State is reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
