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
from typing import Callable, Optional
from urllib.parse import urlparse

from .config_loader import Interests
from .ingest_web import WebFetcher
from .llm import LLMClient
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
class Candidate:
    """A source candidate found in the shared collect phase."""

    kind: str        # 'ig' | 'site'
    key: str         # handle, or https://host
    descriptor: str  # blurb handed to each agent's model for vetting
    signal: str      # follower count / credibility hint
    via: str         # where it was discovered


@dataclass
class DiscoveryCandidates:
    candidates: list[Candidate] = field(default_factory=list)
    ig_lookups: int = 0
    search_queries: int = 0
    budget_hit: bool = False


@dataclass
class DiscoveryResult:
    new_suggestions: int = 0
    ig_lookups: int = 0
    search_queries: int = 0
    budget_hit: bool = False
    candidates_seen: int = 0


def collect_candidates(
    sv: SociaVaultClient,
    fetcher: WebFetcher,
    interests: Interests,
    related: list[tuple[str, RelatedAccount]],
    known_handles: set[str],
    known_hosts: set[str],
    skip: Callable[[str, str], bool],
    max_ig_lookups: int = 12,
    max_search_queries: int = 3,
) -> DiscoveryCandidates:
    """Gather source candidates ONCE per run (the SociaVault-metered part).

    `skip(kind, key)` is True for candidates already tracked or already decided
    on, so we don't spend credits re-checking them. Vetting is left to each agent
    (see vet_candidates) so different models can disagree."""
    res = DiscoveryCandidates()
    seen_handles: set[str] = set()

    def add_ig(handle: str, via: str) -> None:
        if res.ig_lookups >= max_ig_lookups or res.budget_hit:
            return
        if handle in seen_handles or handle in known_handles or skip("ig", handle):
            return
        seen_handles.add(handle)
        try:
            profile = sv.get_instagram_profile(handle)
        except CreditBudgetExceeded:
            log.warning("credit budget reached vetting IG candidate @%s", handle)
            res.budget_hit = True
            return
        except SociaVaultError as exc:
            log.debug("IG candidate lookup failed for @%s: %s", handle, exc)
            return
        res.ig_lookups += 1
        res.candidates.append(Candidate(
            kind="ig", key=handle,
            descriptor=(
                f"Instagram @{profile.username} ({profile.full_name}). "
                f"{profile.followers} followers"
                + (", verified" if profile.is_verified else "")
                + (f". Category: {profile.business_category}"
                   if profile.business_category else "")
                + f". Bio: {profile.biography}"
            ),
            signal=f"{profile.followers} followers",
            via=f"IG related to {via}",
        ))

    # --- Stream 1: IG related accounts ---------------------------------
    for via, ra in related:
        add_ig(ra.handle.lower(), via)

    # --- Stream 2: web search ------------------------------------------
    for query in _build_queries(interests, max_search_queries):
        if res.budget_hit:
            break
        try:
            results = sv.google_search(query)
        except CreditBudgetExceeded:
            log.warning("credit budget reached during web-search discovery")
            res.budget_hit = True
            break
        except SociaVaultError as exc:
            log.error("google_search failed for %r: %s", query, exc)
            continue
        res.search_queries += 1

        for sr in results:
            m = _IG_URL_RE.search(sr.url)
            if m:
                handle = m.group(1).lower().strip("/")
                if handle and handle not in _IG_RESERVED:
                    add_ig(handle, "web search")
                continue

            host = _strip_www((urlparse(sr.url).hostname or "").lower())
            if not host or _is_excluded_host(host) or host in known_hosts:
                continue
            key = f"https://{host}"
            if skip("site", key):
                continue
            known_hosts.add(host)  # don't re-collect the same host this run
            title, description = _homepage_meta(fetcher, key)
            res.candidates.append(Candidate(
                kind="site", key=key,
                descriptor=(
                    f"Website {host}. Title: {title or sr.title}. "
                    f"Description: {description or sr.snippet}. "
                    f"(Found via search query: {query!r})"
                ),
                signal="", via=f"web search: {query!r}",
            ))

    log.info(
        "Discovery collect: %d candidates (%d ig lookups, %d queries)%s",
        len(res.candidates), res.ig_lookups, res.search_queries,
        " (budget hit)" if res.budget_hit else "",
    )
    return res


def vet_candidates(store: Store, llm: LLMClient, interests: Interests,
                   collected: DiscoveryCandidates) -> DiscoveryResult:
    """Vet shared candidates with THIS agent's model and queue its suggestions."""
    res = DiscoveryResult(
        ig_lookups=collected.ig_lookups,
        search_queries=collected.search_queries,
        budget_hit=collected.budget_hit,
        candidates_seen=len(collected.candidates),
    )
    for cand in collected.candidates:
        if store.is_dismissed_or_tracked(cand.kind, cand.key):
            continue
        verdict = llm.vet_source(cand.descriptor, interests)
        if not (verdict and verdict.on_topic):
            continue
        signal = " — ".join(x for x in (cand.signal, verdict.credibility) if x)
        if store.add_suggestion(
            kind=cand.kind, key=cand.key, reason=verdict.reason,
            signal=signal, discovered_via=cand.via,
        ):
            res.new_suggestions += 1
    log.debug("Discovery vet: %d new suggestions", res.new_suggestions)
    return res


def active_handles(store: Store) -> set[str]:
    return {s["key"].lower() for s in store.active_sources("ig")}


def active_hosts(store: Store) -> set[str]:
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
