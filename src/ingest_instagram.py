"""Instagram ingestion, split into a shared collect phase and a per-agent store.

`collect_instagram` does the network/credit work ONCE per run: one profile call
per handle (1 credit each), returning the parsed profiles plus related accounts
for discovery. `store_instagram` then writes those same posts into each agent's
database, applying that agent's own dedup/cursor — so running N agents costs the
same SociaVault credits as running one.

First run for a handle ingests only the most-recent N posts (unless a global date
floor is set) to avoid backfilling years of history. Degrades on the credit
guardrail: if a profile call would exceed the daily budget, collection stops
cleanly and the run continues with what it has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .sociavault import (
    CreditBudgetExceeded,
    ParsedProfile,
    RelatedAccount,
    SociaVaultClient,
    SociaVaultError,
)
from .store import Store

log = logging.getLogger("agent.ingest_ig")


@dataclass
class IGCollectResult:
    """Result of the shared (once-per-run) fetch phase."""

    profiles: dict[str, ParsedProfile] = field(default_factory=dict)
    related: list[tuple[str, RelatedAccount]] = field(default_factory=list)
    budget_hit: bool = False

    @property
    def handles_processed(self) -> int:
        return len(self.profiles)


@dataclass
class IGIngestResult:
    """Result of storing collected posts into one agent's DB."""

    handles_processed: int = 0
    new_posts: int = 0
    skipped_old: int = 0
    budget_hit: bool = False
    related: list[tuple[str, RelatedAccount]] = field(default_factory=list)


def collect_instagram(client: SociaVaultClient, handles: list[str]) -> IGCollectResult:
    """Fetch every handle's profile once (shared across all agents)."""
    result = IGCollectResult()
    for handle in handles:
        try:
            profile = client.get_instagram_profile(handle)
        except CreditBudgetExceeded:
            log.warning("credit budget reached during IG collection; stopping at %s", handle)
            result.budget_hit = True
            break
        except SociaVaultError as exc:
            log.error("IG profile fetch failed for %s: %s", handle, exc)
            continue

        result.profiles[handle] = profile
        for ra in profile.related_accounts:
            result.related.append((handle, ra))

    log.info(
        "IG collect: %d handles fetched, %d related candidates%s",
        result.handles_processed,
        len(result.related),
        " (budget hit)" if result.budget_hit else "",
    )
    return result


def store_instagram(
    store: Store,
    collected: IGCollectResult,
    first_run_limit: int = 6,
    since_ts: int | None = None,
) -> IGIngestResult:
    """Write collected posts into one agent's DB (its own dedup + cursor)."""
    result = IGIngestResult(budget_hit=collected.budget_hit, related=collected.related)

    for src in store.active_sources("ig"):
        handle = src["key"]
        profile = collected.profiles.get(handle)
        if profile is None:
            continue
        result.handles_processed += 1

        first_run = src["last_seen"] is None
        try:
            cursor_ts = int(src["last_seen"]) if src["last_seen"] else 0
        except (TypeError, ValueError):
            cursor_ts = 0

        posts = sorted(
            profile.posts, key=lambda p: p.taken_at_timestamp or 0, reverse=True
        )
        # When a global date floor is set it is the authoritative first-run gate;
        # otherwise fall back to the most-recent-N limit to avoid backfilling.
        if first_run and since_ts is None:
            posts = posts[:first_run_limit]

        max_ts = cursor_ts
        for post in posts:
            ts = post.taken_at_timestamp or 0
            if since_ts is not None and ts and ts < since_ts:
                result.skipped_old += 1
                continue
            if not first_run and ts and ts <= cursor_ts:
                continue
            if store.insert_ig_post(
                post_id=post.shortcode,
                handle=profile.username or handle,
                url=post.url,
                caption=post.caption,
                ts=post.taken_at_timestamp,
                likes=post.likes,
                comments=post.comments,
            ):
                result.new_posts += 1
            max_ts = max(max_ts, ts)

        store.mark_source_run(src["id"], last_seen=str(max_ts) if max_ts else "0")

    log.debug(
        "IG store: %d handles, %d new posts, %d skipped (before cutoff)",
        result.handles_processed, result.new_posts, result.skipped_old,
    )
    return result
