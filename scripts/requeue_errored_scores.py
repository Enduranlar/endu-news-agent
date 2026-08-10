#!/usr/bin/env python3
"""Re-queue items whose scoring call failed, so the next run scores them again.

When a scoring call fails, `score_items` degrades the whole batch to
not-relevant so one flaky call can't crash a run. Those rows are stored with
`relevant=0, score_reason='error'` — an honest record that the model never
actually judged them.

But `daily` only picks up rows where `relevant IS NULL`, so an errored item is
never retried: it sits at relevant=0 forever and can never appear in a report.
On 2026-08-10 that was 2,674 items across the fleet. This script sets those rows
back to unscored so the next run has another go.

WHY THIS IS OPT-IN, unlike the other scripts here: applying it costs real money
on the next run (every re-queued item is re-scored by an LLM), and re-queueing
before the underlying cause is fixed just burns the same items into errors
again. So it previews by default and writes only with --apply.

Reads the agent list from config/agents.yaml and honours AGENT_STATE_DIR. Agent
databases live under data/agents/<name>.db; if you're on the legacy single-agent
layout it falls back to data/agent.db.

Usage:
  python scripts/requeue_errored_scores.py                    # preview everything
  python scripts/requeue_errored_scores.py --apply            # do it
  python scripts/requeue_errored_scores.py --agent kimi --agent qwen-35b
  python scripts/requeue_errored_scores.py --agent gemini --apply
  python scripts/requeue_errored_scores.py --min-rows 1 --apply

Safety notes:
  - Don't run this while `daily` is running; it writes to the same databases.
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


def agent_dbs(only: list[str]) -> list[tuple[str, Path]]:
    """(name, path) for each configured agent that has a database on disk."""
    try:
        from src.config_loader import load_agents

        agents = [(a.name, Path(a.db_path)) for a in load_agents()]
    except Exception:  # noqa: BLE001 — no agents.yaml: legacy single-agent layout
        agents = []
    if not agents:
        agents = [("(single-agent)", settings.DB_FILE)]
    if only:
        wanted = {n.lower() for n in only}
        missing = wanted - {n.lower() for n, _ in agents}
        for name in sorted(missing):
            print(f"warning: no agent named {name!r} in the config", file=sys.stderr)
        agents = [(n, p) for n, p in agents if n.lower() in wanted]
    return [(n, p) for n, p in agents if p.exists()]


def survey(db: Path) -> dict:
    """Per-table error/scored counts, plus this agent's observed cost per item."""
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
        "--agent", action="append", default=[],
        help="limit to this agent (repeatable; default: every configured agent)",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="actually write. Without this, nothing is modified.",
    )
    ap.add_argument(
        "--min-rows", type=int, default=1,
        help="skip agents with fewer than this many errored rows (default 1)",
    )
    args = ap.parse_args(argv)

    dbs = agent_dbs(args.agent)
    if not dbs:
        print("No agent databases found. Has the agent run yet?", file=sys.stderr)
        return 1

    print(f"state dir: {settings.STATE_DIR}")
    print(
        f"{'agent':16s} {'errored':>8s} {'queued':>7s} {'of total':>9s} "
        f"{'est. re-score':>14s}  note"
    )
    plan, total_rows, total_cost, rows_unpriced = [], 0, 0.0, 0
    for name, db in dbs:
        s = survey(db)
        cost = s["errored"] * s["per_item"]
        selected, note = False, ""
        if s["errored"] < args.min_rows:
            note = "skipped (below --min-rows)" if s["errored"] else "nothing to do"
        elif s["total"] and s["errored"] == s["total"]:
            # Every single row failed. Re-queueing before the cause is fixed
            # just spends the same money to write the same errors back.
            note, selected = "ALL rows errored — confirm the cause is fixed", True
        else:
            selected = True
        if selected:
            plan.append((name, db, s))
            total_rows += s["errored"]
            total_cost += cost
            if not s["per_item"]:
                # No successful scoring call on record => nothing to price it from.
                rows_unpriced += s["errored"]
        print(
            f"{name:16s} {s['errored']:8d} {s['unscored']:7d} {s['total']:9d} "
            f"{('$%.2f' % cost) if s['per_item'] else '     n/a':>14s}  {note}"
        )

    if not plan:
        print("\nNothing to re-queue.")
        return 0

    print(
        f"\n{'WOULD RE-QUEUE' if not args.apply else 'RE-QUEUEING'} "
        f"{total_rows} row(s) across {len(plan)} agent(s)."
    )
    print(
        f"Estimated re-scoring cost: ${total_cost:.2f}"
        + (
            f" — but that EXCLUDES {rows_unpriced} row(s) belonging to agents that "
            "never completed a scoring call, so there is nothing to price them "
            "from. The real total will be higher."
            if rows_unpriced
            else " (from each agent's own observed cost per item)."
        )
    )
    already = sum(s["unscored"] for _, _, s in plan)
    if already:
        print(
            f"Note: {already} row(s) in these agents are already unscored and get "
            "picked up by the next run whether or not you apply this."
        )
    if not args.apply:
        print("\nNothing was modified. Re-run with --apply to write.")
        return 0

    print()
    changed_total = 0
    for name, db, _ in plan:
        changed = requeue(db)
        changed_total += changed
        print(f"  {name:16s} {changed} row(s) re-queued")
    print(
        f"\nRe-queued {changed_total} row(s). The next `daily` run will score them.\n"
        "Deploy the relevant fixes first, or they will fail the same way again."
    )
    if settings.STATE_DIR != settings.ROOT:
        print("Remember to sync the state repo (scripts/sync_state.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
