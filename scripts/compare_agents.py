#!/usr/bin/env python3
"""Compare how the fleet's agents judged the same items.

READ-ONLY. Every database is opened with mode=ro. This script never writes to an
agent database, never ingests, never scores, never spends a SociaVault credit and
never makes an LLM call. Running it costs nothing.

WHAT IT COMPARES, AND WHAT IT DELIBERATELY IGNORES

An item can end up relevant=0 for three different reasons, and `score_reason`
records which:

    'model'          the model judged it and said no       <- comparable
    'memory_repeat'  already reported; the memory layer suppressed it
    'error'          the scoring call failed; nobody judged it
    NULL             never scored at all

Only 'model' rows are judgments. Mixing the others in would measure dedup
behaviour and provider reliability instead of model opinion — and the difference
is not small: on 2026-08-10 two agents had 100% error rows, which a naive query
would have reported as the two strictest filters in the fleet. So the comparison
universe is score_reason='model', and everything else is surfaced in the coverage
check instead.

Usage:
  python scripts/compare_agents.py
  python scripts/compare_agents.py --since 2026-08-11
  python scripts/compare_agents.py --agent haiku --agent opus
  python scripts/compare_agents.py --csv /tmp/wide.csv --json
  python scripts/compare_agents.py --limit 100        # more solo catches

Reads the agent list from config/agents.yaml and honours AGENT_STATE_DIR. Agents
that still have a database but are no longer in agents.yaml are skipped by
default -- a retired agent's history stops the day it was retired, so it never
judged anything ingested since, and those items drop out of the common set for
everyone else. --include-parked brings them back; naming one with --agent
always works. Dates are Europe/Istanbul.
"""

from __future__ import annotations

import argparse
import csv as csvmod
import json as jsonmod
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

# Allow running directly: `python scripts/compare_agents.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings  # noqa: E402  (after sys.path tweak)
from src.timeutil import ISTANBUL  # noqa: E402

TABLES = ("ig_posts", "web_items")
KEYCOL = {"ig_posts": "post_id", "web_items": "item_id"}
JUDGED = "model"


# --- loading -----------------------------------------------------------------


def discover_agents(
    only: list[str], include_parked: bool
) -> tuple[list[tuple[str, Path]], list[str]]:
    """((name, db) for each agent to compare, names of parked agents skipped).

    "Parked" = a database on disk whose agent is no longer in agents.yaml.
    Those are excluded by default, because a retired agent's history stops at
    the day it was retired and every item ingested since is one the current
    fleet judged and it didn't — which drops out of the common set and shrinks
    the comparison for everyone else. nemotron-free ran once before being
    removed and was costing 42% of the comparable items.

    Naming a parked agent with --agent still includes it: an explicit request
    beats the default.
    """
    configured: list[tuple[str, Path]] = []
    try:
        from src.config_loader import load_agents

        configured = [(a.name, Path(a.db_path)) for a in load_agents()]
    except Exception:  # noqa: BLE001 — no agents.yaml => legacy single-agent layout
        configured = []
    known = {p.name for _, p in configured}
    want = {n.lower() for n in only}
    pairs, skipped = list(configured), []
    agents_dir = settings.DATA_DIR / "agents"
    if agents_dir.is_dir():
        for db in sorted(agents_dir.glob("*.db")):
            if db.name in known:
                continue
            if include_parked or db.stem.lower() in want:
                pairs.append((db.stem, db))
            else:
                skipped.append(db.stem)
    if not pairs and settings.DB_FILE.exists():
        pairs = [("(single-agent)", settings.DB_FILE)]
    if only:
        for miss in sorted(want - {n.lower() for n, _ in pairs}):
            print(f"warning: no agent named {miss!r}", file=sys.stderr)
        pairs = [(n, p) for n, p in pairs if n.lower() in want]
    return [(n, p) for n, p in pairs if p.exists()], skipped


def since_to_utc(since: str | None) -> str | None:
    """'YYYY-MM-DD' (Istanbul, start of day) -> UTC ISO for string comparison."""
    if not since:
        return None
    day = datetime.strptime(since.strip(), "%Y-%m-%d").replace(tzinfo=ISTANBUL)
    return day.astimezone(timezone.utc).isoformat()


def load_agent(db: Path, cutoff: str | None) -> dict:
    """Verdicts plus the coverage breakdown that makes them interpretable."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        out = {
            "verdict": {},                 # key -> 1/0   (score_reason='model')
            "category": {},                # key -> category
            "meta": {},                    # key -> (source, title, url)
            "counts": Counter(),
            "cost": 0.0,
        }
        for table in TABLES:
            keycol = KEYCOL[table]
            title = "handle" if table == "ig_posts" else "title"
            where, params = "", []
            if cutoff:
                where, params = " WHERE ingested_at >= ?", [cutoff]
            try:
                rows = con.execute(
                    f"SELECT {keycol} AS k, relevant, score_reason, category, "
                    f"url, {title} AS title FROM {table}{where}",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                continue                    # table/column absent (older DB)
            for r in rows:
                key = (table, r["k"])
                reason = r["score_reason"]
                if r["relevant"] is None:
                    out["counts"]["unscored"] += 1
                    continue
                reason = reason or JUDGED   # legacy rows predate score_reason
                out["counts"][reason] += 1
                if reason != JUDGED:
                    continue
                out["verdict"][key] = int(r["relevant"])
                out["category"][key] = r["category"]
                out["meta"][key] = (
                    "ig" if table == "ig_posts" else "web",
                    (r["title"] or "")[:70],
                    r["url"] or "",
                )
        try:
            q = "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage"
            if cutoff:
                q += " WHERE ts >= ?"
                out["cost"] = con.execute(q, [cutoff]).fetchone()[0]
            else:
                out["cost"] = con.execute(q).fetchone()[0]
        except sqlite3.OperationalError:
            out["cost"] = 0.0
        return out
    finally:
        con.close()


# --- statistics --------------------------------------------------------------


def kappa(a: dict, b: dict, keys: list) -> tuple[float, float | None]:
    """(raw agreement, Cohen's kappa). Kappa is None when it's undefined.

    Raw agreement is inflated whenever one verdict dominates, which it does here
    — most items are not relevant. Kappa corrects for agreement expected by
    chance, so the two belong side by side.
    """
    n = len(keys)
    if not n:
        return 0.0, None
    agree = sum(1 for k in keys if a[k] == b[k]) / n
    pa = sum(a[k] for k in keys) / n
    pb = sum(b[k] for k in keys) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    if chance >= 1.0:
        return agree, None            # both flagged everything (or nothing)
    return agree, (agree - chance) / (1 - chance)


def bar(n: int, peak: int, width: int = 42) -> str:
    return "#" * max(1, round(width * n / peak)) if n else ""


# --- report sections ---------------------------------------------------------


def section_coverage(data: dict, names: list[str]) -> set:
    """Print coverage first — everything below is meaningless without it."""
    print("=" * 78)
    print("1. COVERAGE — which items each agent actually judged")
    print("=" * 78)
    union = set()
    for n in names:
        union |= set(data[n]["verdict"])
    print(f"\n{'agent':16s} {'judged':>7s} {'of union':>9s} {'repeat':>7s} "
          f"{'ERROR':>7s} {'unscored':>9s}  gap")
    complete = set(union)
    for n in names:
        d = data[n]
        judged = set(d["verdict"])
        complete &= judged
        miss = len(union) - len(judged)
        c = d["counts"]
        flag = ""
        if union and not judged:
            flag = "  <-- NO DATA AT ALL"
        elif miss > len(union) * 0.10:
            flag = f"  <-- MISSING {miss} ({100*miss/len(union):.0f}%)"
        elif miss:
            flag = f"  missing {miss}"
        print(f"{n:16s} {len(judged):7d} {len(union):9d} {c['memory_repeat']:7d} "
              f"{c['error']:7d} {c['unscored']:9d}{flag}")
    print(f"\nunion of judged items: {len(union)}")
    print(f"judged by EVERY agent: {len(complete)}"
          f"  <- the only fair basis for comparison")
    dropped = len(union) - len(complete)
    if dropped:
        print(f"excluded from the stats below: {dropped} item(s) that at least one "
              "agent never judged")
    # One agent with a short history can shrink the common set for everybody.
    # Worth naming, because the fix is a --agent filter rather than a smaller
    # comparison — and it's invisible from the table above.
    if len(names) > 2 and dropped:
        gains = []
        for n in names:
            others = [set(data[o]["verdict"]) for o in names if o != n]
            gains.append((len(set.intersection(*others)) - len(complete), n))
        gain, worst = max(gains)
        if gain > max(20, 0.15 * len(complete)):
            print(f"\nnote: dropping '{worst}' would raise the common set from "
                  f"{len(complete)} to {len(complete)+gain} items "
                  f"(+{100*gain/max(1,len(complete)):.0f}%).")
            print(f"      Re-run without it if its history is shorter than the "
                  f"rest:\n      --agent " +
                  " --agent ".join(n for n in names if n != worst))
    return complete


def section_histogram(data: dict, names: list[str], common: set) -> dict:
    print("\n" + "=" * 78)
    print("2. FLAG-COUNT DISTRIBUTION — how many agents called each item relevant")
    print("=" * 78)
    if not common:
        print("\n(no items judged by every agent)")
        return {}
    counts = Counter(sum(data[n]["verdict"][k] for n in names) for k in common)
    peak = max(counts.values())
    total = len(common)
    print(f"\n{len(names)} agents, {total} items judged by all of them\n")
    for i in range(len(names) + 1):
        c = counts.get(i, 0)
        label = f"{i}/{len(names)}"
        note = ""
        if i == 0:
            note = "  nobody wanted it"
        elif i == len(names):
            note = "  unanimous keep"
        elif i == 1:
            note = "  solo catch"
        print(f"  {label:>6s} {c:6d} ({100*c/total:5.1f}%) {bar(c, peak):43s}{note}")
    unan = counts.get(0, 0) + counts.get(len(names), 0)
    print(f"\nunanimous (all agree either way): {unan}/{total} = {100*unan/total:.1f}%")
    print(f"contested (at least one dissent):  {total-unan}/{total} = "
          f"{100*(total-unan)/total:.1f}%")
    return {str(i): counts.get(i, 0) for i in range(len(names) + 1)}


def section_per_agent(data: dict, names: list[str], common: set) -> list[dict]:
    print("\n" + "=" * 78)
    print("3. PER AGENT")
    print("=" * 78)
    print(f"\n{'agent':16s} {'judged':>7s} {'relevant':>9s} {'rate':>7s} "
          f"{'common':>7s} {'rate':>7s} {'cost':>9s} {'$/relev':>9s}")
    rows = []
    for n in names:
        d = data[n]
        j = len(d["verdict"])
        rel = sum(d["verdict"].values())
        crel = sum(d["verdict"][k] for k in common) if common else 0
        cost = d["cost"]
        rows.append({
            "agent": n, "judged": j, "relevant": rel,
            "rate": (rel / j) if j else None,
            "common_relevant": crel,
            "common_rate": (crel / len(common)) if common else None,
            "cost_usd": round(cost, 4),
            "cost_per_relevant": round(cost / rel, 6) if rel else None,
        })
        print(f"{n:16s} {j:7d} {rel:9d} {(f'{100*rel/j:.1f}%' if j else 'n/a'):>7s} "
              f"{crel:7d} {(f'{100*crel/len(common):.1f}%' if common else 'n/a'):>7s} "
              f"${cost:8.4f} {(f'${cost/rel:.4f}' if rel else 'n/a'):>9s}")
    print("\n'common' columns use only items judged by every agent, so the rates "
          "are directly comparable.")
    print("Cost is the agent's whole LLM bill (scoring, vetting, summaries), not "
          "scoring alone.")
    return rows


def section_pairwise(data: dict, names: list[str]) -> list[dict]:
    print("\n" + "=" * 78)
    print("4. PAIRWISE AGREEMENT (on items both agents judged)")
    print("=" * 78)
    print(f"\n{'pair':38s} {'n':>6s} {'agree':>7s} {'kappa':>7s}  reading")
    out = []
    for x, y in combinations(names, 2):
        a, b = data[x]["verdict"], data[y]["verdict"]
        keys = [k for k in a if k in b]
        ag, kp = kappa(a, b, keys)
        if kp is None:
            reading = "undefined"
        elif kp > 0.95:
            reading = "identical"
        elif kp >= 0.80:
            reading = "near-identical"
        elif kp >= 0.60:
            reading = "substantial"
        elif kp >= 0.40:
            reading = "moderate"
        elif kp >= 0.20:
            reading = "fair"
        else:
            reading = "poor"
        out.append({"a": x, "b": y, "n": len(keys), "agreement": round(ag, 4),
                    "kappa": round(kp, 4) if kp is not None else None})
        print(f"{x + ' vs ' + y:38s} {len(keys):6d} {100*ag:6.1f}% "
              f"{(f'{kp:.3f}' if kp is not None else '  n/a'):>7s}  {reading}")
    print("\nA pair running the SAME model is your noise floor: its disagreement "
          "is what\none model scores against itself, and no cross-model number "
          "means anything\nuntil it clears that.")
    return out


def section_solo(data: dict, names: list[str], common: set, limit: int) -> list[dict]:
    print("\n" + "=" * 78)
    print(f"5. SOLO CATCHES — flagged by exactly one agent (showing up to {limit})")
    print("=" * 78)
    solo = []
    for k in common:
        flaggers = [n for n in names if data[n]["verdict"][k]]
        if len(flaggers) == 1:
            n = flaggers[0]
            src, title, url = data[n]["meta"].get(k, ("?", "", ""))
            solo.append({"agent": n, "source": src, "title": title, "url": url})
    if not solo:
        print("\n(none)")
        return solo
    by_agent = Counter(s["agent"] for s in solo)
    print(f"\n{len(solo)} solo catch(es):  " +
          ", ".join(f"{a}={c}" for a, c in by_agent.most_common()))
    print("\nWorth reading by hand — a solo catch is either the agent seeing "
          "something\nthe others missed, or noise the others correctly ignored. "
          "No statistic decides\nwhich; only you can.\n")
    for s in sorted(solo, key=lambda s: s["agent"])[:limit]:
        print(f"  [{s['agent']:14s}] ({s['source']}) {s['title']}")
        print(f"   {' ' * 17}{s['url']}")
    if len(solo) > limit:
        print(f"\n  ... {len(solo)-limit} more (use --limit)")
    return solo


def section_categories(data: dict, names: list[str], common: set) -> dict:
    print("\n" + "=" * 78)
    print("6. CATEGORY DISAGREEMENT (both agents flagged it relevant)")
    print("=" * 78)
    pairs, rows = 0, []
    for x, y in combinations(names, 2):
        both = [k for k in common
                if data[x]["verdict"][k] and data[y]["verdict"][k]]
        if not both:
            continue
        diff = sum(1 for k in both
                   if data[x]["category"].get(k) != data[y]["category"].get(k))
        rows.append({"a": x, "b": y, "both_flagged": len(both), "different": diff,
                     "rate": round(diff / len(both), 4)})
        pairs += 1
    if not rows:
        print("\n(no item was flagged by two agents)")
        return {}
    print(f"\n{'pair':38s} {'both':>6s} {'differ':>7s} {'rate':>7s}")
    for r in sorted(rows, key=lambda r: -r["rate"]):
        print(f"{r['a'] + ' vs ' + r['b']:38s} {r['both_flagged']:6d} "
              f"{r['different']:7d} {100*r['rate']:6.1f}%")
    worst = max(rows, key=lambda r: r["rate"])
    print(f"\nAgents agreeing an item matters but not what it IS affects only "
          f"which section\nof the report it lands in. Widest gap: "
          f"{worst['a']} vs {worst['b']} at {100*worst['rate']:.1f}%.")
    return {"pairs": rows}


def write_csv(path: Path, data: dict, names: list[str], common: set) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csvmod.writer(fh)
        w.writerow(["source", "key", "title", "url", "flag_count"]
                   + [f"{n}_relevant" for n in names]
                   + [f"{n}_category" for n in names])
        for k in sorted(common, key=lambda k: (k[0], str(k[1]))):
            meta = next((data[n]["meta"][k] for n in names if k in data[n]["meta"]),
                        ("?", "", ""))
            flags = [data[n]["verdict"][k] for n in names]
            w.writerow([meta[0], k[1], meta[1], meta[2], sum(flags)] + flags
                       + [data[n]["category"].get(k, "") for n in names])
    print(f"\nwrote {path} ({len(common)} rows)")


# --- main --------------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Compare agent scoring across the fleet (read-only)."
    )
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="only items ingested on/after this date (Europe/Istanbul)")
    ap.add_argument("--agent", action="append", default=[],
                    help="limit to this agent (repeatable)")
    ap.add_argument("--limit", type=int, default=50,
                    help="max solo catches to list (default 50)")
    ap.add_argument("--csv", metavar="PATH", help="dump the wide item x agent table")
    ap.add_argument("--json", action="store_true",
                    help="print a machine-readable summary instead of the report")
    ap.add_argument("--include-parked", action="store_true",
                    help="also compare agents that have a database but are no "
                         "longer in agents.yaml (excluded by default: a retired "
                         "agent's history ends early and shrinks the common set)")
    args = ap.parse_args(argv)

    try:
        cutoff = since_to_utc(args.since)
    except ValueError:
        print(f"bad --since {args.since!r}; expected YYYY-MM-DD", file=sys.stderr)
        return 2

    agents, parked = discover_agents(args.agent, args.include_parked)
    if not agents:
        print("No agent databases found. Has the fleet run yet?", file=sys.stderr)
        return 1

    data = {name: load_agent(db, cutoff) for name, db in agents}
    names = [n for n, _ in agents]
    empty = [n for n in names if not data[n]["verdict"]]
    live = [n for n in names if data[n]["verdict"]]

    if not args.json:
        print(f"state dir : {settings.STATE_DIR}")
        print(f"agents    : {len(names)}"
              + (f"  ({len(empty)} with no judged items: "
                 f"{', '.join(empty)})" if empty else ""))
        print(f"window    : {args.since or 'all time'}")
        if parked:
            print(f"skipped   : {', '.join(parked)} — has data but is no longer "
                  "in agents.yaml (--include-parked to compare anyway)")
        print()

    if len(live) < 2:
        sys.stdout.flush()   # keep the message below the context it refers to
        print("Need at least two agents with judged items to compare.",
              file=sys.stderr)
        return 1

    if args.json:
        common = set.intersection(*(set(data[n]["verdict"]) for n in live))
        union = set().union(*(set(data[n]["verdict"]) for n in live))
        summary = {
            "since": args.since,
            "agents": live,
            "agents_without_data": empty,
            "union_items": len(union),
            "common_items": len(common),
            "flag_distribution": {
                str(i): sum(1 for k in common
                            if sum(data[n]["verdict"][k] for n in live) == i)
                for i in range(len(live) + 1)
            },
            "per_agent": [
                {"agent": n, "judged": len(data[n]["verdict"]),
                 "relevant": sum(data[n]["verdict"].values()),
                 "cost_usd": round(data[n]["cost"], 4),
                 "errors": data[n]["counts"]["error"],
                 "memory_repeats": data[n]["counts"]["memory_repeat"],
                 "unscored": data[n]["counts"]["unscored"]}
                for n in live
            ],
            "pairwise": [
                {"a": x, "b": y, "n": len(ks),
                 "agreement": round(ag, 4),
                 "kappa": round(kp, 4) if kp is not None else None}
                for x, y in combinations(live, 2)
                for ks in [[k for k in data[x]["verdict"] if k in data[y]["verdict"]]]
                for ag, kp in [kappa(data[x]["verdict"], data[y]["verdict"], ks)]
            ],
        }
        print(jsonmod.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    common = section_coverage(data, live)
    section_histogram(data, live, common)
    section_per_agent(data, live, common)
    section_pairwise(data, live)
    section_solo(data, live, common, args.limit)
    section_categories(data, live, common)
    if args.csv:
        write_csv(Path(args.csv), data, live, common)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
