"""Source discovery — proposes new IG accounts and websites for operator approval.

Two candidate streams, both ending in the `suggestions` queue (status=pending,
never auto-added):
  1. IG related accounts harvested during IG ingestion.
  2. Web-search candidates from a small rotating query set built from the interest
     list + brand context.

Each fresh candidate gets a lightweight lookup (IG: one profile call; site:
homepage title/description) and an LLM vetting verdict (on-topic? + one-sentence
reason + credibility note). Accepted candidates are inserted as pending.

Discovery is the lowest-priority credit consumer: it stops cleanly when the daily
budget is reached. Dedup is aggressive — `suggestions` has a UNIQUE(kind,key) and
we never resurface a candidate the operator has dismissed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

from .config_loader import Interests
from .ingest_web import WebFetcher
from .llm import LLMClient
from .sociavault import (
    CreditBudgetExceeded,
    RelatedAccount,
    SociaVaultClient,
    SociaVaultError,
)
from .store import Store
from .timeutil import istanbul_now

log = logging.getLogger("agent.discovery")

_IG_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)")
_IG_RESERVED = {"p", "reel", "reels", "explore", "stories", "tv", "accounts"}


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


@dataclass
class DiscoveryResult:
    new_suggestions: int = 0
    ig_lookups: int = 0
    search_queries: int = 0
    budget_hit: bool = False
    candidates_seen: int = 0


def run_discovery(
    store: Store,
    sv: SociaVaultClient,
    fetcher: WebFetcher,
    llm: LLMClient,
    interests: Interests,
    related: list[tuple[str, RelatedAccount]],
    max_ig_lookups: int = 12,
    max_search_queries: int = 3,
) -> DiscoveryResult:
    result = DiscoveryResult()

    active_handles = {s["key"].lower() for s in store.active_sources("ig")}
    active_hosts = _active_hosts(store)

    # --- Stream 1: IG related accounts ---------------------------------
    seen_handles: set[str] = set()
    for via, ra in related:
        if result.ig_lookups >= max_ig_lookups:
            break
        handle = ra.handle.lower()
        if (
            handle in seen_handles
            or handle in active_handles
            or store.is_dismissed_or_tracked("ig", handle)
        ):
            continue
        seen_handles.add(handle)
        result.candidates_seen += 1
        if _vet_ig_candidate(
            store, sv, llm, interests, handle, via, result
        ) is False and result.budget_hit:
            break

    # --- Stream 2: web search ------------------------------------------
    queries = _build_queries(interests, max_search_queries)
    for query in queries:
        if result.budget_hit:
            break
        try:
            results = sv.google_search(query)
        except CreditBudgetExceeded:
            log.warning("credit budget reached during web-search discovery")
            result.budget_hit = True
            break
        except SociaVaultError as exc:
            log.error("google_search failed for %r: %s", query, exc)
            continue
        result.search_queries += 1

        for sr in results:
            host = _strip_www((urlparse(sr.url).hostname or "").lower())
            # IG handles surfaced in search results.
            m = _IG_URL_RE.search(sr.url)
            if m:
                handle = m.group(1).lower().strip("/")
                if (
                    handle
                    and handle not in _IG_RESERVED
                    and handle not in active_handles
                    and not store.is_dismissed_or_tracked("ig", handle)
                    and result.ig_lookups < max_ig_lookups
                ):
                    result.candidates_seen += 1
                    _vet_ig_candidate(
                        store, sv, llm, interests, handle, "web search", result
                    )
                continue

            # Website candidates.
            if not host or _is_excluded_host(host):
                continue
            key = f"https://{host}"
            if host in active_hosts or store.is_dismissed_or_tracked("site", key):
                continue
            active_hosts.add(host)  # don't re-vet same host twice this run
            result.candidates_seen += 1
            _vet_site_candidate(
                store, fetcher, llm, interests, host, key, sr, query, result
            )

    log.info(
        "Discovery: %d candidates, %d ig lookups, %d queries, %d new suggestions%s",
        result.candidates_seen,
        result.ig_lookups,
        result.search_queries,
        result.new_suggestions,
        " (budget hit)" if result.budget_hit else "",
    )
    return result


def _vet_ig_candidate(
    store: Store,
    sv: SociaVaultClient,
    llm: LLMClient,
    interests: Interests,
    handle: str,
    via: str,
    result: DiscoveryResult,
) -> Optional[bool]:
    try:
        profile = sv.get_instagram_profile(handle)
    except CreditBudgetExceeded:
        log.warning("credit budget reached vetting IG candidate @%s", handle)
        result.budget_hit = True
        return False
    except SociaVaultError as exc:
        log.debug("IG candidate lookup failed for @%s: %s", handle, exc)
        return None
    result.ig_lookups += 1

    descriptor = (
        f"Instagram @{profile.username} ({profile.full_name}). "
        f"{profile.followers} followers"
        + (", verified" if profile.is_verified else "")
        + (f". Category: {profile.business_category}" if profile.business_category else "")
        + f". Bio: {profile.biography}"
    )
    verdict = llm.vet_source(descriptor, interests)
    if verdict and verdict.on_topic:
        added = store.add_suggestion(
            kind="ig",
            key=handle,
            reason=verdict.reason,
            signal=f"{profile.followers} followers — {verdict.credibility}",
            discovered_via=f"IG related to {via}",
        )
        if added:
            result.new_suggestions += 1
    return None


def _vet_site_candidate(
    store: Store,
    fetcher: WebFetcher,
    llm: LLMClient,
    interests: Interests,
    host: str,
    key: str,
    sr,
    query: str,
    result: DiscoveryResult,
) -> None:
    title, description = _homepage_meta(fetcher, key)
    descriptor = (
        f"Website {host}. Title: {title or sr.title}. "
        f"Description: {description or sr.snippet}. "
        f"(Found via search query: {query!r})"
    )
    verdict = llm.vet_source(descriptor, interests)
    if verdict and verdict.on_topic:
        added = store.add_suggestion(
            kind="site",
            key=key,
            reason=verdict.reason,
            signal=verdict.credibility,
            discovered_via=f"web search: {query!r}",
        )
        if added:
            result.new_suggestions += 1


def _active_hosts(store: Store) -> set[str]:
    hosts: set[str] = set()
    for kind in ("rss", "site"):
        for s in store.active_sources(kind):
            host = _strip_www((urlparse(s["key"]).hostname or "").lower())
            if host:
                hosts.add(host)
    return hosts


def _build_queries(interests: Interests, limit: int) -> list[str]:
    """Build a small rotating query set from the interest list + brand context.

    Rotation is deterministic by ISO week so each run probes a different slice
    without a second search vendor or random state."""
    pool = [
        "endurance sports nutrition new product launch",
        "ultra marathon Türkiye 2026 yarış takvimi",
        "trail running race results 2026",
        "marathon world record broken",
        "triathlon news athlete nutrition",
        "energy gel hydration new product endurance",
        "Türkiye maraton koşu haberleri",
        "ironman triathlon 2026 results",
        "running fueling strategy news",
        "cycling endurance nutrition brand",
    ]
    week = int(istanbul_now().strftime("%V"))
    start = (week * limit) % len(pool)
    rotated = pool[start:] + pool[:start]
    return rotated[:limit]


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            d = dict(attrs)
            name = (d.get("name") or d.get("property") or "").lower()
            if name in ("description", "og:description") and d.get("content"):
                if not self.description:
                    self.description = d["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()


def _homepage_meta(fetcher: WebFetcher, url: str) -> tuple[str, str]:
    html = fetcher.get_text(url)
    if not html:
        return "", ""
    p = _MetaParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        pass
    return p.title[:200], p.description[:400]


def _is_excluded_host(host: str) -> bool:
    # Social platforms, search engines, marketplaces, encyclopaedias — not news
    # sources we'd track. Entries ending in '.' match any TLD as a prefix.
    exact = (
        "instagram.com",
        "facebook.com",
        "youtube.com",
        "youtu.be",
        "twitter.com",
        "x.com",
        "tiktok.com",
        "google.com",
        "wikipedia.org",
        "linkedin.com",
        "reddit.com",
        "medium.com",
    )
    prefixes = ("amazon.", "pinterest.", "google.")
    if any(host == e or host.endswith("." + e) for e in exact):
        return True
    return any(host == p[:-1] or host.startswith(p) for p in prefixes)
