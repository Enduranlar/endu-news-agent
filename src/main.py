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
    load_interests,
    load_web_sources,
)
from .logging_setup import setup_logging
from .store import Store
from .timeutil import cutoff_unix, istanbul_day, istanbul_weekday_name

log = logging.getLogger("agent.main")


# --- daily -------------------------------------------------------------------


def cmd_daily(args: argparse.Namespace) -> int:
    from .discovery import run_discovery
    from .ingest_instagram import ingest_instagram
    from .ingest_web import WebFetcher, ingest_web
    from .llm import LLMClient
    from .races import run_race_tracking
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

    with Store() as store:
        ig_accounts = load_ig_accounts()
        web_sources = load_web_sources()
        interests = load_interests()
        rec = store.reconcile_sources(ig_accounts, web_sources)
        log.info(
            "sources reconciled: +%d new, %d reactivated, %d deactivated",
            rec.added,
            rec.reactivated,
            rec.deactivated,
        )

        tracker = CreditTracker(store, cfg.sociavault_daily_credit_budget, day)
        llm = LLMClient(cfg.anthropic_api_key, cfg.llm_filter_model, cfg.llm_summary_model)

        new_ig = 0
        related = []
        budget_hit = False
        skipped_old = 0

        def _track_races(fetcher):
            return run_race_tracking(
                store, fetcher, llm, interests, today, since_floor
            )

        race_res = None

        with WebFetcher(cfg.outbound_proxy_url or None) as fetcher:
            # IG ingestion (credit-spending) — skipped in dry-run.
            if dry:
                log.info("dry-run: skipping IG ingestion (credit-spending)")
            else:
                with SociaVaultClient(cfg.sociavault_api_key, tracker) as sv:
                    ig_res = ingest_instagram(
                        store, sv, cfg.ig_first_run_limit, since_ts=since_ts
                    )
                    new_ig = ig_res.new_posts
                    related = ig_res.related
                    budget_hit = ig_res.budget_hit
                    skipped_old += ig_res.skipped_old

                    # Web ingestion (free).
                    web_res = ingest_web(
                        store,
                        fetcher,
                        cfg.web_first_run_limit,
                        cfg.site_max_new_per_run,
                        since_ts=since_ts,
                    )

                    # Turkish race calendar tracking (free; LLM for results only).
                    race_res = _track_races(fetcher)

                    # Relevance scoring (LLM).
                    rel = score_pending(store, llm, interests)

                    # Discovery (credit-spending, lowest priority).
                    disc = run_discovery(
                        store, sv, fetcher, llm, interests, related
                    )
                    budget_hit = budget_hit or disc.budget_hit
                    new_suggestions = disc.new_suggestions
            if dry:
                web_res = ingest_web(
                    store,
                    fetcher,
                    cfg.web_first_run_limit,
                    cfg.site_max_new_per_run,
                    since_ts=since_ts,
                )
                race_res = _track_races(fetcher)
                rel = score_pending(store, llm, interests)
                new_suggestions = 0

        skipped_old += web_res.skipped_old

        # Retention purge.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=cfg.raw_item_retention_days)
        ).isoformat()
        purged = store.purge_old_raw_items(cutoff)

        races_new = race_res.new_races if race_res else 0
        race_results_found = race_res.results_found if race_res else 0

        log.info(
            "RUN SUMMARY (daily%s): new_ig=%d new_web=%d skipped_old=%d scored=%d "
            "relevant=%d new_suggestions=%d races_new=%d race_results=%d "
            "credits_spent=%d/%d purged=%d%s",
            " dry-run" if dry else "",
            new_ig,
            web_res.new_items,
            skipped_old,
            rel.scored,
            rel.relevant,
            new_suggestions,
            races_new,
            race_results_found,
            tracker.spent(),
            cfg.sociavault_daily_credit_budget,
            purged,
            " [BUDGET HIT]" if budget_hit else "",
        )
    return 0


# --- report ------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    from .archive import archive_report
    from .emailer import EmailError, send_report
    from .llm import LLMClient
    from .report import build_report

    period = args.period
    dry = args.dry_run
    cfg = settings.load_settings(require_email=not dry, require_llm=True)
    day = istanbul_day()

    with Store() as store:
        if not dry and store.report_already_sent_today(period, day) and not args.force:
            log.info("report %s already sent today (%s); skipping (use --force)", period, day)
            return 0

        interests = load_interests()
        llm = LLMClient(cfg.anthropic_api_key, cfg.llm_filter_model, cfg.llm_summary_model)
        bundle = build_report(store, llm, interests, period)

        archived = archive_report(bundle)
        report_id = store.create_report(period, archived.relpath, bundle.item_count)
        log.info("report built: %d items, %d categories, archived %s",
                 bundle.item_count, len(bundle.categories_covered), archived.relpath)

        if dry:
            print(bundle.markdown)
            log.info("dry-run: report not emailed, sent_at not recorded")
            return 0

        try:
            send_report(cfg, bundle.title, bundle.html, bundle.markdown)
        except EmailError as exc:
            log.error("EMAIL FAILED: %s (report kept at %s)", exc, archived.path)
            return 2  # non-zero so cron mail surfaces it; sent_at NOT recorded

        store.mark_report_sent(report_id)
        log.info("report %s sent and recorded", period)
    return 0


# --- discover (standalone) ---------------------------------------------------


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
    with Store() as store:
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
    with Store() as store:
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


def cmd_dismiss(args: argparse.Namespace) -> int:
    with Store() as store:
        ok = store.set_suggestion_status(args.id, "dismissed")
        if ok:
            print(f"Suggestion #{args.id} dismissed; it will never resurface.")
            return 0
        print(f"No suggestion with id {args.id}.", file=sys.stderr)
        return 1


# --- status ------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    with Store() as store:
        counts = store.counts()
        day = istanbul_day()
        budget_env = settings_int_safe("SOCIAVAULT_DAILY_CREDIT_BUDGET", 400)
        spent = store.credits_spent_today(day)

        print(f"=== Endurance News Agent status ({day}, Europe/Istanbul) ===")
        print(f"Credits spent today: {spent} / {budget_env}")
        print()
        print("DB counts:")
        for k, v in counts.items():
            print(f"  {k:24s}: {v}")
        print()
        print("Active sources (last run):")
        for s in store.active_sources():
            label = f"@{s['key']}" if s["kind"] == "ig" else s["key"]
            print(f"  [{s['kind']:4s}] {label}  last_run={s['last_run_at'] or 'never'}")
        last_sent = store.last_report_sent_at()
        print()
        print(f"Last report sent: {last_sent or 'never'}")
    return 0


def settings_int_safe(name: str, default: int) -> int:
    import os

    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# --- test-sociavault (Milestone 1) -------------------------------------------


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
    p_daily.set_defaults(func=cmd_daily)

    p_report = sub.add_parser("report", help="build + email the summary")
    p_report.add_argument("--period", required=True, choices=["monday", "friday"])
    p_report.add_argument("--dry-run", action="store_true",
                          help="build + archive but don't email")
    p_report.add_argument("--force", action="store_true",
                          help="send even if one was already sent today")
    p_report.set_defaults(func=cmd_report)

    p_disc = sub.add_parser("discover", help="run source discovery standalone")
    p_disc.add_argument("--dry-run", action="store_true")
    p_disc.set_defaults(func=cmd_discover)

    p_sug = sub.add_parser("suggestions", help="list pending suggestions")
    p_sug.set_defaults(func=cmd_suggestions)

    p_app = sub.add_parser("approve", help="approve a suggestion")
    g = p_app.add_mutually_exclusive_group(required=True)
    g.add_argument("--ig", metavar="HANDLE", help="approve an IG handle")
    g.add_argument("--site", metavar="URL", help="approve a website/feed URL")
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

    p_dis = sub.add_parser("dismiss", help="dismiss a suggestion")
    p_dis.add_argument("--id", type=int, required=True)
    p_dis.set_defaults(func=cmd_dismiss)

    p_stat = sub.add_parser("status", help="print run/credit/DB status")
    p_stat.set_defaults(func=cmd_status)

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
