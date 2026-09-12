#!/usr/bin/env python3
"""Re-queue items whose scoring call failed, so the next run scores them again.

When a scoring call fails, `score_items` degrades the whole batch to
not-relevant so one flaky call can't crash a run. Those rows are stored with
`relevant=0, score_reason='error'` — an honest record that the model never
actually judged them.

But `daily` only picks up rows where `relevant IS NULL`, so an errored item is
never retried: it sits at relevant=0 forever and can never appear in a report.
On 2026-08-10 that was 2,674 items across the then-running fleet. This script
sets those rows
back to unscored so the next run has another go.

WHY THIS IS OPT-IN, unlike the other scripts here: applying it costs real money
on the next run (every re-queued item is re-scored by an LLM), and re-queueing
before the underlying cause is fixed just burns the same items into errors
again. So it previews by default and writes only with --apply.

Honours AGENT_STATE_DIR; operates on data/agent.db.

Usage:
  python scripts/requeue_errored_scores.py            # preview
  python scripts/requeue_errored_scores.py --apply    # do it

Safety notes:
  - Don't run this while `daily` is running; it writes to the same database.
  - The state repo is version-controlled, so `git -C <state> checkout -- data/`
    restores the databases if you change your mind.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running directly: `python scripts/requeue_errored_scores.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings  # noqa: E402  (after sys.path tweak)

TABLES = ("ig_posts", "web_items")


def target_db() -> Path:
    """The agent's database."""
    return settings.DB_FILE


def survey(db: Path) -> dict:
    """Per-table error/scored counts, plus the observed cost per scored item."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {"errors": {}, "total": 0, "errored": 0, "judged": 0, "unscored": 0}
        for table in TABLES:
            try:
                n_err = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE score_reason='error'"
                ).fetchone()[0]
                n_all = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                n_ok = con.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    "WHERE score_reason IN ('model','memory_repeat')"
                ).fetchone()[0]
                n_null = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE relevant IS NULL"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue  # table or column missing (older DB)
            out["errors"][table] = n_err
            out["total"] += n_all
            out["errored"] += n_err
            out["judged"] += n_ok
            out["unscored"] += n_null
        try:
            spent = con.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE call_type='score'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            spent = 0.0
        out["per_item"] = (spent / out["judged"]) if out["judged"] else 0.0
        return out
    finally:
        con.close()


def requeue(db: Path) -> int:
    """Set errored rows back to unscored. Returns rows changed."""
    con = sqlite3.connect(db)
    try:
        changed = 0
        for table in TABLES:
            try:
                cur = con.execute(
                    f"UPDATE {table} SET relevant=NULL, score_reason=NULL "
                    "WHERE score_reason='error'"
                )
            except sqlite3.OperationalError:
                continue
            changed += cur.rowcount
        con.commit()
        return changed
    finally:
        con.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Re-queue items whose scoring call failed (previews by default)."
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="actually write. Without this, nothing is modified.",
    )
    args = ap.parse_args(argv)

    db = target_db()
    if not db.exists():
        print(f"No database at {db}. Has the agent run yet?", file=sys.stderr)
        return 1

    s = survey(db)
    cost = s["errored"] * s["per_item"]
    print(f"database : {db}")
    print(f"errored  : {s['errored']}   (of {s['total']} rows)")
    print(f"already unscored: {s['unscored']}  — the next run picks these up "
          "whether or not you apply this")
    if s["per_item"]:
        print(f"estimated re-scoring cost: ${cost:.2f} "
              "(from the observed cost per scored item)")
    elif s["errored"]:
        print("estimated re-scoring cost: unknown — no successful scoring call "
              "on record to price it from")

    if not s["errored"]:
        print("\nNothing to re-queue.")
        return 0
    if s["total"] and s["errored"] == s["total"]:
        print("\nWARNING: every row errored. That means the agent is broken, not "
              "unlucky —\nconfirm the cause is fixed first or this just writes "
              "the same errors back.")

    if not args.apply:
        print("\nNothing was modified. Re-run with --apply to write.")
        return 0

    changed = requeue(db)
    print(f"\nRe-queued {changed} row(s). The next `daily` run will score them.")
    print("Deploy the relevant fixes first, or they will fail the same way again.")
    if settings.STATE_DIR != settings.ROOT:
        print("Remember to sync the state repo (scripts/sync_state.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
