"""Detect whether a website exposes an RSS/Atom feed, and add it to websites.md.

Given a URL, this:
  1. Normalises it (adds https:// if missing).
  2. Checks if the URL *is* itself a feed.
  3. Looks for `<link rel="alternate" type="application/rss+xml|atom+xml">` tags.
  4. Falls back to probing a handful of common feed paths (/feed, /rss.xml, ...).
  5. Validates candidates with feedparser (must parse as a real feed).

If a feed is found it's written as `rss | <feed-url>`; otherwise the site is
written as `site | <url>` for the raw-site crawler. Turkish (.tr) hosts are
fetched through the proxy, consistent with the rest of the agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser

from .config_loader import append_web_source
from .ingest_web import WebFetcher

log = logging.getLogger("agent.feed_detect")

# Probed only if no <link> feed tag is found. Root-relative.
COMMON_FEED_PATHS = (
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/feeds/posts/default",
    "/blog/feed/",
    "/news/feed/",
    "/?feed=rss2",
)


@dataclass
class FeedDetection:
    site_url: str
    kind: str  # 'rss' | 'site'
    feed_url: Optional[str] = None
    title: str = ""
    detected_via: str = "none"  # 'self' | 'link-tag' | 'probe' | 'none'


@dataclass
class AddResult:
    detection: FeedDetection
    added: bool
    line: str


class _FeedLinkParser(HTMLParser):
    """Collects feed <link> alternates and the page <title>."""

    def __init__(self) -> None:
        super().__init__()
        self.feeds: list[str] = []  # ordered, rss preferred over atom
        self._atoms: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        if tag != "link":
            return
        d = {k.lower(): (v or "") for k, v in attrs}
        rel = d.get("rel", "").lower()
        typ = d.get("type", "").lower()
        href = d.get("href", "")
        if not href or "alternate" not in rel:
            return
        if "rss" in typ or "rdf" in typ:
            self.feeds.append(href)
        elif "atom" in typ:
            self._atoms.append(href)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()

    def ordered_candidates(self) -> list[str]:
        # RSS first, then Atom — both are handled by feedparser downstream.
        seen: set[str] = set()
        out: list[str] = []
        for href in self.feeds + self._atoms:
            if href not in seen:
                seen.add(href)
                out.append(href)
        return out


def normalize_url(url: str) -> str:
    url = url.strip()
    if not urlparse(url).scheme:
        url = "https://" + url
    return url


def _is_valid_feed(text: Optional[str]) -> tuple[bool, str]:
    """Return (is_feed, title). A valid feed has a recognised feedparser version."""
    if not text:
        return False, ""
    try:
        parsed = feedparser.parse(text)
    except Exception:  # noqa: BLE001
        return False, ""
    version = getattr(parsed, "version", "") or ""
    has_entries = bool(getattr(parsed, "entries", []))
    if version or has_entries:
        title = ""
        feed = getattr(parsed, "feed", {}) or {}
        if isinstance(feed, dict):
            title = feed.get("title", "") or ""
        return True, title.strip()
    return False, ""


def detect_feed(url: str, fetcher: WebFetcher) -> FeedDetection:
    site_url = normalize_url(url)
    log.info("detecting feed for %s", site_url)

    # 1. Is the URL itself a feed?
    body = fetcher.get_text(site_url)
    is_feed, feed_title = _is_valid_feed(body)
    if is_feed:
        return FeedDetection(
            site_url=site_url,
            kind="rss",
            feed_url=site_url,
            title=feed_title,
            detected_via="self",
        )

    # 2. <link rel="alternate"> feed tags in the page head.
    page_title = ""
    if body:
        parser = _FeedLinkParser()
        try:
            parser.feed(body)
        except Exception:  # noqa: BLE001 — malformed HTML shouldn't crash
            pass
        page_title = parser.title
        for href in parser.ordered_candidates():
            candidate = urljoin(site_url, href)
            ok, t = _is_valid_feed(fetcher.get_text(candidate))
            if ok:
                return FeedDetection(
                    site_url=site_url,
                    kind="rss",
                    feed_url=candidate,
                    title=t or page_title,
                    detected_via="link-tag",
                )

    # 3. Probe common feed paths at the site root.
    root = f"{urlparse(site_url).scheme}://{urlparse(site_url).netloc}"
    tried: set[str] = set()
    for path in COMMON_FEED_PATHS:
        candidate = urljoin(root + "/", path.lstrip("/"))
        if candidate in tried or candidate == site_url:
            continue
        tried.add(candidate)
        ok, t = _is_valid_feed(fetcher.get_text(candidate))
        if ok:
            return FeedDetection(
                site_url=site_url,
                kind="rss",
                feed_url=candidate,
                title=t or page_title,
                detected_via="probe",
            )

    # 4. Nothing — track as a raw site.
    return FeedDetection(
        site_url=site_url,
        kind="site",
        feed_url=None,
        title=page_title,
        detected_via="none",
    )


def add_website(
    url: str,
    note: str = "",
    proxy: Optional[str] = None,
    websites_path: Optional[Path] = None,
    fetcher: Optional[WebFetcher] = None,
) -> AddResult:
    """Detect a feed for `url` and append the right line to websites.md.

    Returns an AddResult describing the detection and whether a line was added
    (False if the source was already tracked).
    """
    own_fetcher = fetcher is None
    fetcher = fetcher or WebFetcher(proxy or None)
    try:
        detection = detect_feed(url, fetcher)
    finally:
        if own_fetcher:
            fetcher.close()

    if detection.kind == "rss":
        target = detection.feed_url or detection.site_url
        added = append_web_source("rss", target, note, websites_path)
        line = f"rss | {target}" + (f" | {note}" if note else "")
    else:
        target = detection.site_url
        added = append_web_source("site", target, note, websites_path)
        line = f"site | {target}" + (f" | {note}" if note else "")

    return AddResult(detection=detection, added=added, line=line)
