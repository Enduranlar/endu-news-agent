#!/usr/bin/env python3
"""Commit + push the state repo (config, reports, data/agent.db).

Run after each agent job so operator state is version-controlled in its own repo,
separate from the code. Requires AGENT_STATE_DIR to point at a git clone of your
state repo (see the README). Idempotent: a no-op when nothing changed.

On first run it drops a .gitignore (transient SQLite sidecars) and .gitattributes
(agent.db marked binary) into the state repo. Before committing it checkpoints the
SQLite WAL so the committed agent.db is a complete, consistent snapshot.

Usage:
  python scripts/sync_state.py                 # checkpoint, commit, push
  python scripts/sync_state.py --no-push       # commit only
  python scripts/sync_state.py --dry-run       # show what would be committed
  python scripts/sync_state.py -m "message"    # custom commit message

For cron/systemd, git push must work non-interactively (SSH deploy key or a
credential helper) — see the README.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

# Allow running directly: `python scripts/sync_state.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings  # noqa: E402
from src.timeutil import istanbul_now  # noqa: E402

_GITIGNORE = (
    "# Transient SQLite sidecars — the committed agent.db is the snapshot.\n"
    "data/*.db-wal\n"
    "data/*.db-shm\n"
    "data/*.db-journal\n"
)
_GITATTRIBUTES = "data/agent.db binary\n"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Commit + push the state repo.")
    ap.add_argument("-m", "--message", default=None, help="commit message")
    ap.add_argument("--no-push", action="store_true", help="commit but don't push")
    ap.add_argument("--dry-run", action="store_true", help="show changes, do nothing")
    args = ap.parse_args(argv)

    state = settings.STATE_DIR
    if state == settings.ROOT:
        print(
            "AGENT_STATE_DIR is not set, so state lives in the code repo — nothing "
            "separate to sync.\nSet AGENT_STATE_DIR to your state-repo clone "
            "(see the README).",
            file=sys.stderr,
        )
        return 2
    if not (state / ".git").exists():
        print(
            f"{state} is not a git repo. Clone your state repo there first "
            "(see the README).",
            file=sys.stderr,
        )
        return 2

    # Ensure helper files exist (idempotent).
    gi = state / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE, encoding="utf-8")
    ga = state / ".gitattributes"
    if not ga.exists():
        ga.write_text(_GITATTRIBUTES, encoding="utf-8")

    # Checkpoint the WAL so the committed agent.db is a complete snapshot.
    db = settings.DB_FILE
    if db.exists():
        try:
            con = sqlite3.connect(db)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            con.close()
        except sqlite3.Error as exc:
            print(f"warning: WAL checkpoint failed: {exc}", file=sys.stderr)

    _git(["git", "add", "-A"], state)
    if _git(["git", "diff", "--cached", "--quiet"], state).returncode == 0:
        print("state repo: nothing to commit.")
        return 0

    if args.dry_run:
        status = _git(["git", "status", "--short"], state)
        print("[dry-run] would commit:\n" + status.stdout, end="")
        return 0

    msg = args.message or f"state {istanbul_now().strftime('%Y-%m-%d %H:%M')}"
    commit = _git(["git", "commit", "-m", msg], state)
    if commit.returncode != 0:
        print("git commit failed:\n" + commit.stdout + commit.stderr, file=sys.stderr)
        return 1
    print(f"committed: {msg}")

    if args.no_push:
        return 0
    push = _git(["git", "push"], state)
    if push.returncode != 0:
        print("git push failed:\n" + push.stdout + push.stderr, file=sys.stderr)
        return 1
    print("pushed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
