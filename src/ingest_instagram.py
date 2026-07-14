"""Instagram ingestion.

For each active handle: fetch the profile (1 credit), insert any new posts (keyed
by shortcode), advance the per-handle timestamp cursor, and harvest related
accounts into the discovery candidate pool. First run for a handle ingests only
the most-recent N posts to avoid backfilling years of history.

Degrades on the credit guardrail: if a profile call would exceed the daily
budget, ingestion stops cleanly and the run continues with what it has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .sociavault import (
    CreditBudgetExceeded,
    RelatedAccount,
    SociaVaultClient,
    SociaVaultError,
)
from .store import Store

log = logging.getLogger("agent.ingest_ig")


@dataclass
class IGIngestResult:
    handles_processed: int = 0
    new_posts: int = 0
    skipped_old: int = 0
    budget_hit: bool = False
    related: list[tuple[str, RelatedAccount]] = field(default_factory=list)
    # related is (discovered_via_handle, RelatedAccount)


def ingest_instagram(
    store: Store,
    client: SociaVaultClient,
    first_run_limit: int = 6,
    since_ts: int | None = None,
) -> IGIngestResult:
    result = IGIngestResult()
    sources = store.active_sources("ig")

    for src in sources:
        handle = src["key"]
        try:
            profile = client.get_instagram_profile(handle)
        except CreditBudgetExceeded:
            log.warning("credit budget reached during IG ingestion; stopping at %s", handle)
            result.budget_hit = True
            break
        except SociaVaultError as exc:
            log.error("IG profile fetch failed for %s: %s", handle, exc)
            continue

        result.handles_processed += 1
        first_run = src["last_seen"] is None
        try:
            cursor_ts = int(src["last_seen"]) if src["last_seen"] else 0
        except (TypeError, ValueError):
            cursor_ts = 0

        posts = sorted(
            profile.posts,
            key=lambda p: p.taken_at_timestamp or 0,
            reverse=True,
        )
        # When a global date floor is set it is the authoritative first-run gate;
        # otherwise fall back to the most-recent-N limit to avoid backfilling.
        if first_run and since_ts is None:
            posts = posts[:first_run_limit]

        max_ts = cursor_ts
        for post in posts:
            ts = post.taken_at_timestamp or 0
            # Global ingest floor: drop anything dated before the cutoff.
            if since_ts is not None and ts and ts < since_ts:
                result.skipped_old += 1
                continue
            # Skip anything we've already processed (cursor) — dedup also covers it.
            if not first_run and ts and ts <= cursor_ts:
                continue
            inserted = store.insert_ig_post(
                post_id=post.shortcode,
                handle=profile.username or handle,
                url=post.url,
                caption=post.caption,
                ts=post.taken_at_timestamp,
                likes=post.likes,
                comments=post.comments,
            )
            if inserted:
                result.new_posts += 1
            max_ts = max(max_ts, ts)

        # Advance cursor to newest ts seen (string for sources.last_seen).
        store.mark_source_run(src["id"], last_seen=str(max_ts) if max_ts else "0")

        for ra in profile.related_accounts:
            result.related.append((handle, ra))

    log.info(
        "IG ingest: %d handles, %d new posts, %d skipped (before cutoff), "
        "%d related candidates%s",
        result.handles_processed,
        result.new_posts,
        result.skipped_old,
        len(result.related),
        " (budget hit)" if result.budget_hit else "",
    )
    return result
