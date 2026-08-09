"""CLI entrypoint for the endurance news agent.

Commands:
  daily              Ingest IG + web, score relevance, run discovery (cron 06:30).
  report --period    Build + archive + email the Mon/Fri summary.
  discover           Run source discovery on its own (uses harvested IG relateds
                     only when run inside `daily`; standalone uses web search).
  suggestions        List pending source suggestions.
  approve --ig/--site  Approve a suggestion → append to the config file.
  dismiss --id       Dismiss a suggestion (never resurfaced).
  status             Print last-run times, today's credit spend, DB counts.
  test-sociavault    Milestone-1 live profile call for one handle (field-path check).

Timing lives in cron, not here (see README / §10). --dry-run skips email and
credit-spending SociaVault calls so the operator can test safely.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import date, datetime, timedelta, timezone

from . import settings
from .config_loader import (
    append_ig_account,
    append_web_source,
    load_ig_accounts,
    load_agents,
    load_interests,
    load_memory_config,
    load_web_sources,
)
from .logging_setup import setup_logging
from .store import Store
from .timeutil import cutoff_unix, istanbul_day, istanbul_weekday_name

log = logging.getLogger("agent.main")


# --- daily -------------------------------------------------------------------


def _fleet():
    """Agent list without needing full settings (used by operator commands)."""
    return load_agents(env_default=("", ""))


def _pick_agent(name):
    """Resolve --agent NAME, defaulting to the primary agent."""
    agents = _fleet()
    if name:
        for a in agents:
            if a.name == name:
                return a
        raise settings.ConfigError(
            f"unknown agent {name!r}; enabled: {', '.join(a.name for a in agents)}"
        )
    return next((a for a in agents if a.primary), agents[0])


def _make_llm(cfg, agent, store):
    """Build the model client for one agent (OpenRouter unless pinned/absent)."""
    from .llm import LLMClient

    provider = agent.provider or cfg.default_provider
    key = cfg.api_key_for(provider)
    if not key:
        env = "OPENROUTER_API_KEY" if provider == "openrouter" else "ANTHROPIC_API_KEY"
        raise settings.ConfigError(f"agent {agent.name!r} needs {env} for {provider}")
    return LLMClient(
        api_key=key,
        filter_model=agent.filter_model or cfg.llm_filter_model,
        summary_model=agent.report_model or cfg.llm_summary_model,
        provider=provider, base_url=cfg.openrouter_base_url,
        store=store, agent=agent.name,
    )


def cmd_daily(args: argparse.Namespace) -> int:
    """Shared collect (SociaVault + web, ONCE) then per-agent scoring in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    from .discovery import (
        active_handles, active_hosts, collect_candidates, vet_candidates,
    )
    from .ingest_instagram import collect_instagram, store_instagram
    from .ingest_web import WebFetcher, collect_web, store_web
    from .races import CALENDAR_URL, run_race_tracking
    from .relevance import score_pending
    from .sociavault import CreditTracker, SociaVaultClient

    cfg = settings.load_settings(require_email=False, require_llm=True)
    day = istanbul_day()
    today = date.fromisoformat(day)
    since_floor = (
        date.fromisoformat(cfg.ingest_since_date) if cfg.ingest_since_date else None
    )
    dry = args.dry_run
    since_ts = cutoff_unix(cfg.ingest_since_date)
    if since_ts is not None:
        log.info("ingest floor active: ignoring items before %s", cfg.ingest_since_date)

    ig_accounts = load_ig_accounts()
    web_sources = load_web_sources()
    interests = load_interests()
    memory = load_memory_config()
    agents = load_agents(env_default=(cfg.llm_filter_model, cfg.llm_summary_model))
    if args.agent:
        agents = [a for a in agents if a.name in set(args.agent)]
        if not agents:
            log.error("no enabled agent matches %s", ", ".join(args.agent))
            return 1
    primary = next((a for a in agents if a.primary), agents[0])
    log.info(
        "fleet: %s (primary: %s)",
        ", ".join(f"{a.name}={a.model}" for a in agents), primary.name,
    )

    settings.ensure_dirs()
    (settings.DATA_DIR / "agents").mkdir(parents=True, exist_ok=True)

    # --- Phase 1: shared collect (network + SociaVault credits, ONCE) --------
    shared = Store(settings.SHARED_DB_FILE)
    tracker = CreditTracker(shared, cfg.sociavault_daily_credit_budget, day)
    ig_collected = None
    web_collected: list = []
    calendar_html = None
    candidates = None

    # Reconcile into the primary DB first so discovery can see what's tracked
    # (config is shared, so any agent's view would do).
    with Store(primary.db_path) as pstore:
        pstore.reconcile_sources(ig_accounts, web_sources)
        known_handles = active_handles(pstore)
        known_hosts = active_hosts(pstore)
        skip = pstore.is_dismissed_or_tracked

        with WebFetcher(cfg.outbound_proxy_url or None) as fetcher:
            web_collected = collect_web(
                fetcher, web_sources, shared,
                cfg.web_first_run_limit, cfg.site_max_new_per_run, since_ts=since_ts,
            )
            calendar_html = fetcher.get_text(CALENDAR_URL)
            if dry:
                log.info("dry-run: skipping SociaVault calls (credit-spending)")
            else:
                with SociaVaultClient(cfg.sociavault_api_key, tracker) as sv:
                    ig_collected = collect_instagram(
                        sv, [a.handle for a in ig_accounts]
                    )
                    candidates = collect_candidates(
                        sv, fetcher, interests, ig_collected.related,
                        known_handles, known_hosts, skip,
                    )

    # --- Phase 2: per-agent work, in parallel --------------------------------
    def run_agent(agent) -> dict:
        # Each thread opens its own Store (SQLite connections aren't shareable),
        # its own fetcher and model client; agents write to separate DB files.
        out = {"agent": agent.name}
        with Store(agent.db_path) as store, WebFetcher(
            cfg.outbound_proxy_url or None
        ) as agent_fetcher:
            llm = _make_llm(cfg, agent, store)
            try:
                store.reconcile_sources(ig_accounts, web_sources)
                ig_res = (
                    store_instagram(store, ig_collected, cfg.ig_first_run_limit, since_ts)
                    if ig_collected else None
                )
                web_res = store_web(store, web_collected)
                race_res = run_race_tracking(
                    store, agent_fetcher, llm, interests, today, since_floor,
                    calendar_html=calendar_html,
                )
                rel = score_pending(store, llm, interests, memory)
                disc = (
                    vet_candidates(store, llm, interests, candidates)
                    if candidates else None
                )
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(days=cfg.raw_item_retention_days)
                ).isoformat()
                out.update(
                    new_ig=ig_res.new_posts if ig_res else 0,
                    skipped_old=ig_res.skipped_old if ig_res else 0,
                    new_web=web_res.new_items,
                    scored=rel.scored, relevant=rel.relevant,
                    repeats=rel.repeats, remembered=rel.remembered,
                    suggestions=disc.new_suggestions if disc else 0,
                    races_new=race_res.new_races,
                    race_results=race_res.results_found,
                    purged=store.purge_old_raw_items(cutoff),
                    mem_purged=store.purge_expired_memory(),
                    cost=store.cost_totals(since_day=day),
                )
            finally:
                llm.close()
        return out

    results: list[dict] = []
    if len(agents) == 1:
        results.append(run_agent(agents[0]))
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(agents))) as pool:
            futures = {pool.submit(run_agent, a): a for a in agents}
            for fut, a in futures.items():
                try:
                    results.append(fut.result())
                except Exception:  # noqa: BLE001 — one agent must not sink the run
                    log.error("agent %s failed:\n%s", a.name, traceback.format_exc())

    # --- Phase 3: summary ----------------------------------------------------
    for r in sorted(results, key=lambda x: x["agent"]):
        c = r.get("cost") or {}
        log.info(
            "RUN SUMMARY (daily%s) [%s]: new_ig=%d new_web=%d skipped_old=%d "
            "scored=%d relevant=%d repeats=%d remembered=%d new_suggestions=%d "
            "races_new=%d race_results=%d purged=%d/%d llm=%d calls/$%.4f",
            " dry-run" if dry else "", r["agent"],
            r.get("new_ig", 0), r.get("new_web", 0), r.get("skipped_old", 0),
            r.get("scored", 0), r.get("relevant", 0), r.get("repeats", 0),
            r.get("remembered", 0), r.get("suggestions", 0),
            r.get("races_new", 0), r.get("race_results", 0),
            r.get("purged", 0), r.get("mem_purged", 0),
            int(c.get("calls") or 0), float(c.get("cost") or 0.0),
        )
    log.info(
        "credits spent today: %d/%d%s",
        tracker.spent(), cfg.sociavault_daily_credit_budget,
        " [BUDGET HIT]" if (ig_collected and ig_collected.budget_hit) else "",
    )
    shared.close()
    return 0


# --- report ------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    """Build + archive + email one report PER agent."""
    from .archive import archive_report
    from .emailer import EmailError, send_report
    from .report import build_report

    period = args.period
    dry = args.dry_run
    cfg = settings.load_settings(require_email=not dry, require_llm=True)
    day = istanbul_day()
    interests = load_interests()
    agents = load_agents(env_default=(cfg.llm_filter_model, cfg.llm_summary_model))
    if args.agent:
        agents = [a for a in agents if a.name in set(args.agent)]
        if not agents:
            log.error("no enabled agent matches %s", ", ".join(args.agent))
            return 1

    failures = 0
    for agent in agents:
        with Store(agent.db_path) as store:
            if not dry and store.report_already_sent_today(period, day) and not args.force:
                log.info("[%s] report %s already sent today (%s); skipping (use --force)",
                         agent.name, period, day)
                continue

            llm = _make_llm(cfg, agent, store)
            try:
                bundle = build_report(
                    store, llm, interests, period,
                    agent="" if agent.legacy else agent.name,
                )
            finally:
                llm.close()

            archived = archive_report(bundle, agent.reports_dir)
            report_id = store.create_report(period, archived.relpath, bundle.item_count)
            log.info("[%s] report built: %d items, %d categories, archived %s",
                     agent.name, bundle.item_count,
                     len(bundle.categories_covered), archived.relpath)

            if dry:
                print(bundle.markdown)
                log.info("[%s] dry-run: not emailed, sent_at not recorded", agent.name)
                continue

            try:
                send_report(cfg, bundle.title, bundle.html, bundle.markdown)
            except EmailError as exc:
                log.error("[%s] EMAIL FAILED: %s (report kept at %s)",
                          agent.name, exc, archived.path)
                failures += 1
                continue

            store.mark_report_sent(report_id)
            log.info("[%s] report %s sent and recorded", agent.name, period)

    return 2 if failures else 0   # non-zero so cron surfaces send failures


# --- discover (standalone) ---------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    from .discovery import (
        active_handles, active_hosts, collect_candidates, vet_candidates,
    )
    from .ingest_web import WebFetcher
    from .sociavault import CreditTracker, SociaVaultClient

    cfg = settings.load_settings(require_email=False, require_llm=True)
    if args.dry_run:
        log.info("dry-run: discovery skipped (credit-spending)")
        return 0

    interests = load_interests()
    agents = load_agents(env_default=(cfg.llm_filter_model, cfg.llm_summary_model))
    primary = next((a for a in agents if a.primary), agents[0])
    shared = Store(settings.SHARED_DB_FILE)
    tracker = CreditTracker(shared, cfg.sociavault_daily_credit_budget)

    with Store(primary.db_path) as pstore:
        with WebFetcher(cfg.outbound_proxy_url or None) as fetcher, SociaVaultClient(
            cfg.sociavault_api_key, tracker
        ) as sv:
            candidates = collect_candidates(
                sv, fetcher, interests, [],
                active_handles(pstore), active_hosts(pstore),
                pstore.is_dismissed_or_tracked,
            )

    for agent in agents:
        with Store(agent.db_path) as store:
            llm = _make_llm(cfg, agent, store)
            try:
                disc = vet_candidates(store, llm, interests, candidates)
            finally:
                llm.close()
            log.info("[%s] discovery: %d new suggestions",
                     agent.name, disc.new_suggestions)
    shared.close()
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from .discovery import run_discovery
    from .ingest_web import WebFetcher
    from .llm import LLMClient
    from .sociavault import CreditTracker, SociaVaultClient

    cfg = settings.load_settings(require_email=False, require_llm=True)
    if args.dry_run:
        log.info("dry-run: discovery skipped (credit-spending)")
        return 0

    with Store() as store:
        interests = load_interests()
        tracker = CreditTracker(store, cfg.sociavault_daily_credit_budget)
        llm = LLMClient(cfg.anthropic_api_key, cfg.llm_filter_model, cfg.llm_summary_model)
        with WebFetcher(cfg.outbound_proxy_url or None) as fetcher, SociaVaultClient(
            cfg.sociavault_api_key, tracker
        ) as sv:
            disc = run_discovery(store, sv, fetcher, llm, interests, related=[])
        log.info("discovery: %d new suggestions", disc.new_suggestions)
    return 0


# --- suggestions / approve / dismiss -----------------------------------------


def cmd_suggestions(args: argparse.Namespace) -> int:
    with Store(_pick_agent(getattr(args, "agent", None)).db_path) as store:
        rows = store.pending_suggestions()
        if not rows:
            print("No pending suggestions.")
            return 0
        print(f"{len(rows)} pending suggestion(s):\n")
        for s in rows:
            label = f"@{s['key']}" if s["kind"] == "ig" else s["key"]
            print(f"[#{s['id']}] {label} ({s['kind']})")
            print(f"      reason: {s['reason']}")
            print(f"      signal: {s['signal']}")
            print(f"      via:    {s['discovered_via']}")
            if s["kind"] == "ig":
                print(f"      approve: python -m src.main approve --ig {s['key']}")
            else:
                print(f"      approve: python -m src.main approve --site {s['key']}")
            print(f"      dismiss: python -m src.main dismiss --id {s['id']}\n")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    # Approving appends to the SHARED config file; the status is marked in this
    # agent's DB (the web UI marks it across the whole fleet).
    with Store(_pick_agent(getattr(args, "agent", None)).db_path) as store:
        if args.ig:
            handle = args.ig
            added = append_ig_account(handle)
            store.set_suggestion_status_by_key("ig", handle.lstrip("@").lower(), "approved")
            if added:
                print(f"Approved @{handle} → appended to config/igaccounts.md")
            else:
                print(f"@{handle} already in config/igaccounts.md (suggestion marked approved)")
        elif args.site:
            url = args.site
            kind = args.as_type
            added = append_web_source(kind, url)
            store.set_suggestion_status_by_key("site", url, "approved")
            store.set_suggestion_status_by_key("rss", url, "approved")
            if added:
                print(f"Approved {url} ({kind}) → appended to config/websites.md")
            else:
                print(f"{url} already in config/websites.md (suggestion marked approved)")
        else:
            print("approve requires --ig <handle> or --site <url>", file=sys.stderr)
            return 1
    print("It will be tracked on the next `daily` run (sources reconcile then).")
    return 0


def cmd_add_site(args: argparse.Namespace) -> int:
    """Detect a feed for a URL and append it to websites.md (no API keys needed)."""
    import os

    from dotenv import load_dotenv

    from .feed_detect import add_website

    load_dotenv(settings.ROOT / ".env")
    proxy = os.environ.get("OUTBOUND_PROXY_URL", "").strip() or None
    note = " ".join(args.note) if args.note else ""

    result = add_website(args.url, note=note, proxy=proxy)
    d = result.detection
    if d.kind == "rss":
        print(f"Feed found ({d.detected_via}): {d.feed_url}")
    else:
        print(f"No feed found — tracking as raw site: {d.site_url}")
    if result.added:
        print(f"Added to config/websites.md: {result.line}")
        print("It will be tracked on the next `daily` run.")
    else:
        print(f"Already in config/websites.md: {result.line}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Run the web admin (suggestions review + manual add). No API keys needed."""
    import os

    from dotenv import load_dotenv

    from .webapp import serve

    load_dotenv(settings.ROOT / ".env")
    host = args.host or os.environ.get("WEB_LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = args.port or int(os.environ.get("WEB_LISTEN_PORT", "8765") or "8765")
    proxy = os.environ.get("OUTBOUND_PROXY_URL", "").strip() or None
    if host in ("0.0.0.0", "::"):
        log.warning(
            "WEB_LISTEN_HOST=%s exposes the admin UI on all interfaces and it has "
            "NO auth — bind it to your Tailscale IP instead.",
            host,
        )
    serve(host, port, proxy)
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    """Inspect / prune what the agent remembers across reports."""
    with Store(_pick_agent(getattr(args, "agent", None)).db_path) as store:
        if args.forget is not None:
            ok = store.forget_memory(args.forget)
            print(
                f"Memory #{args.forget} forgotten."
                if ok
                else f"No memory entry with id {args.forget}."
            )
            return 0 if ok else 1

        if args.purge_expired:
            n = store.purge_expired_memory()
            print(f"Purged {n} expired memory entr{'y' if n == 1 else 'ies'}.")
            return 0

        memory = load_memory_config()
        if not memory.enabled:
            print(
                "Memory is disabled (no config/memory.yaml topics). "
                "Copy config/memory.yaml.example to enable it."
            )

        rows = store.list_memory(topic=args.topic, query=args.query or "", limit=args.limit)
        if not rows:
            print("No memory entries.")
            return 0
        print(f"{len(rows)} memory entr{'y' if len(rows) == 1 else 'ies'}:\n")
        for r in rows:
            exp = r["expires_at"] or "never"
            print(f"[#{r['id']}] ({r['topic']}) {r['subject']}")
            print(f"      {r['fact']}")
            print(
                f"      seen {r['times_seen']}x · first {(r['first_seen_at'] or '')[:10]}"
                f" · expires {exp}"
            )
            if r["source_url"]:
                print(f"      {r['source_url']}")
            print(f"      forget: python -m src.main memory --forget {r['id']}\n")
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    with Store(_pick_agent(getattr(args, "agent", None)).db_path) as store:
        ok = store.set_suggestion_status(args.id, "dismissed")
        if ok:
            print(f"Suggestion #{args.id} dismissed; it will never resurface.")
            return 0
        print(f"No suggestion with id {args.id}.", file=sys.stderr)
        return 1


# --- status ------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    day = istanbul_day()
    budget_env = settings_int_safe("SOCIAVAULT_DAILY_CREDIT_BUDGET", 400)
    agents = _fleet()

    # SociaVault credits are global (spent once per run, shared by the fleet).
    shared_path = (
        settings.SHARED_DB_FILE
        if settings.SHARED_DB_FILE.exists()
        else agents[0].db_path
    )
    spent = 0
    if shared_path.exists():
        with Store(shared_path) as shared:
            spent = shared.credits_spent_today(day)

    print(f"=== Endurance News Agent status ({day}, Europe/Istanbul) ===")
    print(f"SociaVault credits today (shared): {spent} / {budget_env}")
    print(f"Agents: {', '.join(a.name for a in agents)}")

    grand = 0.0
    for agent in agents:
        label = f"{agent.name} ({agent.model or 'model from .env'})"
        if not agent.db_path.exists():
            print(f"\n--- {label} ---\n  (no database yet — hasn't run)")
            continue
        with Store(agent.db_path) as store:
            counts = store.counts()
            total = store.cost_totals()
            today_cost = store.cost_totals(since_day=day)
            grand += float(total.get("cost") or 0.0)
            print(f"\n--- {label} ---")
            print(f"  db: {agent.db_path}")
            print(
                f"  llm: {int(total['calls'])} calls, ${float(total['cost']):.4f} total"
                f"  |  today: {int(today_cost['calls'])} calls, "
                f"${float(today_cost['cost']):.4f}"
            )
            for k, v in counts.items():
                print(f"  {k:24s}: {v}")
            print(f"  last report sent        : {store.last_report_sent_at() or 'never'}")

    if len(agents) > 1:
        print(f"\nTOTAL LLM spend (all agents, all time): ${grand:.4f}")

    if getattr(args, "sources", False) and agents[0].db_path.exists():
        with Store(agents[0].db_path) as store:
            print("\nActive sources (last run):")
            for s_ in store.active_sources():
                lbl = f"@{s_['key']}" if s_["kind"] == "ig" else s_["key"]
                print(f"  [{s_['kind']:4s}] {lbl}  last_run={s_['last_run_at'] or 'never'}")
    return 0


def settings_int_safe(name: str, default: int) -> int:
    import os

    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# --- test-sociavault (Milestone 1) -------------------------------------------


def cmd_test_agents(args: argparse.Namespace) -> int:
    """Preflight: check every agent in agents.yaml can actually run.

    Makes one tiny real call per model (fractions of a cent) exercising exactly
    what the pipeline needs — a JSON-schema structured call on the filter model,
    plus a text call on the summary model when it differs. Exits non-zero if any
    agent fails, so it can gate a deploy or run from cron."""
    import time as _time
    from pathlib import Path

    cfg = settings.load_settings(require_email=False, require_llm=True)
    agents = load_agents(env_default=(cfg.llm_filter_model, cfg.llm_summary_model))
    if args.agent:
        agents = [a for a in agents if a.name in set(args.agent)]
        if not agents:
            print("no enabled agent matches " + ", ".join(args.agent), file=sys.stderr)
            return 1

    # In-memory ledger so the check never writes to the real agent databases.
    mem = Store(Path(":memory:"))

    print(f"Checking {len(agents)} agent(s) — one small live call per model.\n")
    rows, failures = [], 0
    for agent in agents:
        provider = agent.provider or cfg.default_provider
        before = float(mem.cost_totals().get("cost") or 0.0)
        try:
            llm = _make_llm(cfg, agent, mem)
        except settings.ConfigError as exc:
            rows.append((agent, provider, None, 0.0, str(exc)))
            failures += 1
            continue
        try:
            res = llm.probe()
        finally:
            llm.close()
        cost = float(mem.cost_totals().get("cost") or 0.0) - before
        rows.append((agent, provider, res, cost, res.error))
        if not res.ok:
            failures += 1

    width = max((len(a.name) for a, *_ in rows), default=5)
    for agent, provider, res, cost, err in rows:
        mark = "OK  " if (res and res.ok) else "FAIL"
        models = agent.model
        if agent.summary_model and agent.summary_model != agent.model:
            models += f"  +  {agent.summary_model}"
        timing = f"{res.latency_s:5.1f}s" if res else "    -"
        print(f"[{mark}] {agent.name:<{width}}  {timing}  ${cost:.5f}  "
              f"{provider}: {models}")
        if err:
            print(f"        └─ {err}")
        elif res and not res.text_ok:
            print("        └─ summary model failed")

    total = sum(c for *_, c, _ in rows)
    print(f"\n{len(rows) - failures}/{len(rows)} agent(s) OK · check cost ${total:.5f}")
    sys.stdout.flush()
    if failures:
        print(
            "\nCommon causes: a model id that doesn't exist on the provider, a model "
            "that can't do structured outputs (JSON schema — required), no credit, "
            "or a missing/invalid API key.",
            file=sys.stderr,
        )
    mem.close()
    return 1 if failures else 0


def cmd_test_races(args: argparse.Namespace) -> int:
    """Fetch the teamrunbo calendar and print parsed TR races (no DB writes)."""
    from .ingest_web import WebFetcher
    from .races import CALENDAR_URL, extract_races

    with WebFetcher(None) as fetcher:
        html = fetcher.get_text(CALENDAR_URL)
    if not html:
        print("calendar fetch failed", file=sys.stderr)
        return 1
    races = extract_races(html)
    tr = [r for r in races if r.is_tr]
    print(f"parsed {len(races)} races total, {len(tr)} Turkish\n")
    show = tr if not args.all else races
    for r in show[: args.limit]:
        flag = "TR" if r.is_tr else "  "
        end = r.end_date.isoformat() if r.end_date else "?"
        print(f"[{flag}] {r.date_raw:>16s} (bitiş {end})  {r.name}")
        print(f"       yer: {r.location} | mesafe: {r.distances} | url: {r.url or '-'}")
    return 0


def cmd_test_sociavault(args: argparse.Namespace) -> int:
    import json

    from .sociavault import SociaVaultClient

    cfg = settings.load_settings(require_email=False, require_llm=False)
    trim = not args.no_trim
    with SociaVaultClient(cfg.sociavault_api_key) as sv:
        if args.raw:
            raw = sv.fetch_profile_raw(args.handle, trim=trim)
            keys = list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__
            print(f"top-level keys: {keys}\n")
            print(json.dumps(raw, indent=2, ensure_ascii=False)[: args.raw_limit])
            print(
                f"\n(raw output truncated to {args.raw_limit} chars; "
                f"--raw-limit to change. Look for where the posts array lives.)"
            )
            return 0
        profile = sv.get_instagram_profile(args.handle, trim=trim)
    print(f"username:   {profile.username}")
    print(f"full_name:  {profile.full_name}")
    print(f"followers:  {profile.followers}")
    print(f"verified:   {profile.is_verified}")
    print(f"bio_links:  {profile.bio_links}")
    print(f"\nposts ({len(profile.posts)}):")
    for p in profile.posts[:10]:
        print(f"  {p.shortcode}  ts={p.taken_at_timestamp}  likes={p.likes}  "
              f"comments={p.comments}  {p.url}")
        if p.caption:
            print(f"      {p.caption[:100]}")
    print(f"\nrelated accounts ({len(profile.related_accounts)}):")
    for ra in profile.related_accounts:
        print(f"  @{ra.handle}  {ra.full_name}")
    return 0


# --- argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="endu-news-agent", description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_daily = sub.add_parser("daily", help="ingest + score + discover")
    p_daily.add_argument("--dry-run", action="store_true",
                         help="skip credit-spending SociaVault calls")
    p_daily.add_argument("--agent", action="append", metavar="NAME",
                         help="run only these agents (repeatable)")
    p_daily.set_defaults(func=cmd_daily)

    p_report = sub.add_parser("report", help="build + email the summary")
    p_report.add_argument("--period", required=True, choices=["monday", "friday"])
    p_report.add_argument("--dry-run", action="store_true",
                          help="build + archive but don't email")
    p_report.add_argument("--force", action="store_true",
                          help="send even if one was already sent today")
    p_report.add_argument("--agent", action="append", metavar="NAME",
                          help="report only for these agents (repeatable)")
    p_report.set_defaults(func=cmd_report)

    p_disc = sub.add_parser("discover", help="run source discovery standalone")
    p_disc.add_argument("--dry-run", action="store_true")
    p_disc.set_defaults(func=cmd_discover)

    p_sug = sub.add_parser("suggestions", help="list pending suggestions")
    p_sug.add_argument("--agent", help="which agent's queue (default: primary)")
    p_sug.set_defaults(func=cmd_suggestions)

    p_app = sub.add_parser("approve", help="approve a suggestion")
    g = p_app.add_mutually_exclusive_group(required=True)
    g.add_argument("--ig", metavar="HANDLE", help="approve an IG handle")
    g.add_argument("--site", metavar="URL", help="approve a website/feed URL")
    p_app.add_argument("--agent", help="which agent's queue (default: primary)")
    p_app.add_argument("--as", dest="as_type", choices=["rss", "site"], default="site",
                       help="for --site: feed type (default site)")
    p_app.set_defaults(func=cmd_approve)

    p_add = sub.add_parser(
        "add-site", help="auto-detect a site's RSS feed and add it to websites.md"
    )
    p_add.add_argument("url", help="website URL")
    p_add.add_argument("note", nargs="*", help="optional inline note")
    p_add.set_defaults(func=cmd_add_site)

    p_web = sub.add_parser(
        "web", help="run the web admin UI for reviewing/adding sources"
    )
    p_web.add_argument("--host", help="bind address (default WEB_LISTEN_HOST or 127.0.0.1)")
    p_web.add_argument("--port", type=int, help="port (default WEB_LISTEN_PORT or 8765)")
    p_web.set_defaults(func=cmd_web)

    p_mem = sub.add_parser("memory", help="inspect what the agent remembers")
    p_mem.add_argument("--agent", help="which agent's memory (default: primary)")
    p_mem.add_argument("--topic", help="filter by memory topic id")
    p_mem.add_argument("--query", help="substring match on subject/fact")
    p_mem.add_argument("--limit", type=int, default=50)
    p_mem.add_argument("--forget", type=int, metavar="ID", help="delete one entry")
    p_mem.add_argument(
        "--purge-expired", action="store_true", help="delete all expired entries"
    )
    p_mem.set_defaults(func=cmd_memory)

    p_dis = sub.add_parser("dismiss", help="dismiss a suggestion")
    p_dis.add_argument("--id", type=int, required=True)
    p_dis.add_argument("--agent", help="which agent's queue (default: primary)")
    p_dis.set_defaults(func=cmd_dismiss)

    p_stat = sub.add_parser("status", help="print run/credit/cost/DB status")
    p_stat.add_argument("--sources", action="store_true", help="also list sources")
    p_stat.set_defaults(func=cmd_status)

    p_agents = sub.add_parser(
        "test-agents", help="check every agent in agents.yaml can actually run"
    )
    p_agents.add_argument("--agent", action="append", metavar="NAME",
                          help="check only these agents (repeatable)")
    p_agents.set_defaults(func=cmd_test_agents)

    p_races = sub.add_parser(
        "test-races", help="fetch + parse the teamrunbo race calendar (no DB writes)"
    )
    p_races.add_argument("--all", action="store_true", help="show non-TR races too")
    p_races.add_argument("--limit", type=int, default=40)
    p_races.set_defaults(func=cmd_test_races)

    p_test = sub.add_parser("test-sociavault", help="Milestone-1 live profile call")
    p_test.add_argument("--handle", required=True)
    p_test.add_argument("--raw", action="store_true",
                        help="dump the raw JSON response instead of parsed output")
    p_test.add_argument("--raw-limit", type=int, default=6000,
                        help="truncate raw dump to N chars (default 6000)")
    p_test.add_argument("--no-trim", action="store_true",
                        help="request the profile without trim=true")
    p_test.set_defaults(func=cmd_test_sociavault)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose)
    try:
        return args.func(args)
    except settings.ConfigError as exc:
        log.error("CONFIG ERROR: %s", exc)
        return 1
    except Exception:  # noqa: BLE001 — log full traceback, exit non-zero for cron
        log.error("UNHANDLED EXCEPTION:\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
