"""Web ingestion: RSS feeds (feedparser) and raw sites (link discovery + extract).

RSS: parse the feed, key entries by link hash, fetch + extract main text with
trafilatura for new links, queue for scoring.

Raw sites: fetch the listing/homepage, extract same-domain article-looking links,
diff against what's already stored, fetch + extract new ones (capped per run,
polite delay).

Turkish sites (.tr domains) are fetched through the Turkish proxy; everything
else goes direct. The crawler is deliberately conservative and respects a small
inter-request delay.
"""

from __future__ import annotations

import calendar
import logging
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
import trafilatura

from .store import Store

log = logging.getLogger("agent.ingest_web")

POLITE_DELAY = 1.0  # seconds between raw-site article fetches
USER_AGENT = (
    "EnduNewsAgent/1.0 (+https://yourstore.com; endurance nutrition news monitor)"
)

# httpx's read timeout is per-chunk, not wall-clock — a server that trickles
# bytes can block indefinitely. We pair an explicit per-op Timeout with a hard
# wall-clock deadline and a response-size cap enforced while streaming, so no
# single URL can stall the whole (sequential) run.
FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
FETCH_DEADLINE_S = 30.0   # absolute wall-clock budget for one URL
FETCH_MAX_BYTES = 8_000_000  # 8 MB cap (feeds/article pages are far smaller)
RSS_FETCH_BUDGET = 25     # max full-article fetches per feed per run

# Path segments that almost never represent an article page.
_NON_ARTICLE_SEGMENTS = {
    "category",
    "categories",
    "tag",
    "tags",
    "author",
    "authors",
    "page",
    "search",
    "login",
    "signup",
    "register",
    "account",
    "cart",
    "shop",
    "product",
    "products",
    "privacy",
    "terms",
    "contact",
    "about",
    "feed",
    "rss",
    "wp-login",
    "wp-admin",
}


@dataclass
class WebIngestResult:
    rss_sources: int = 0
    site_sources: int = 0
    new_items: int = 0
    skipped_old: int = 0


def _entry_ts(entry) -> Optional[int]:
    """Unix timestamp (UTC) of an RSS entry, from published/updated, or None."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return calendar.timegm(st)
            except (TypeError, ValueError):
                continue
    return None


def is_turkish_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(".tr") or ".tr/" in url.lower()


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)


class WebFetcher:
    """Fetches URLs, choosing the proxy for Turkish sites only."""

    def __init__(
        self,
        proxy_url: Optional[str],
        timeout: httpx.Timeout | None = None,
        deadline_s: float = FETCH_DEADLINE_S,
        max_bytes: int = FETCH_MAX_BYTES,
    ):
        self.timeout = timeout or FETCH_TIMEOUT
        self.deadline_s = deadline_s
        self.max_bytes = max_bytes
        headers = {"User-Agent": USER_AGENT}
        self._direct = httpx.Client(
            headers=headers, timeout=self.timeout, follow_redirects=True
        )
        self._proxied = (
            httpx.Client(
                headers=headers,
                timeout=self.timeout,
                follow_redirects=True,
                proxy=proxy_url,
            )
            if proxy_url
            else None
        )

    def close(self) -> None:
        self._direct.close()
        if self._proxied:
            self._proxied.close()

    def __enter__(self) -> "WebFetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_text(self, url: str) -> Optional[str]:
        """Fetch a URL as text, bounded by a hard wall-clock deadline and a size
        cap. Returns None on any error, non-200, timeout, or overrun — a single
        misbehaving URL can never hang the run."""
        client = self._proxied if (self._proxied and is_turkish_url(url)) else self._direct
        deadline = time.monotonic() + self.deadline_s
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    log.debug("fetch %s -> %s", url, resp.status_code)
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    if time.monotonic() > deadline:
                        log.warning(
                            "fetch %s aborted: exceeded %.0fs wall-clock deadline",
                            url,
                            self.deadline_s,
                        )
                        return None
                    total += len(chunk)
                    if total > self.max_bytes:
                        log.warning(
                            "fetch %s truncated at %d bytes", url, self.max_bytes
                        )
                        break
                    chunks.append(chunk)
                encoding = resp.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace")
        except httpx.HTTPError as exc:
            log.debug("fetch error %s: %s", url, exc)
        except Exception as exc:  # noqa: BLE001 — never let one URL crash a run
            log.warning("unexpected fetch error %s: %s", url, exc)
        return None


def ingest_web(
    store: Store,
    fetcher: WebFetcher,
    web_first_run_limit: int = 6,
    site_max_new_per_run: int = 15,
    since_ts: int | None = None,
) -> WebIngestResult:
    result = WebIngestResult()
    for src in store.active_sources("rss"):
        result.rss_sources += 1
        new, skipped = _ingest_rss(
            store, fetcher, src, first_run_limit=web_first_run_limit, since_ts=since_ts
        )
        result.new_items += new
        result.skipped_old += skipped
    for src in store.active_sources("site"):
        result.site_sources += 1
        result.new_items += _ingest_site(
            store, fetcher, src, max_new=site_max_new_per_run
        )
    log.info(
        "Web ingest: %d rss, %d site, %d new items, %d skipped (before cutoff)",
        result.rss_sources,
        result.site_sources,
        result.new_items,
        result.skipped_old,
    )
    return result


def _ingest_rss(
    store: Store, fetcher: WebFetcher, src, first_run_limit: int,
    since_ts: int | None = None,
) -> tuple[int, int]:
    url = src["key"]
    raw = fetcher.get_text(url)
    if raw is None:
        log.warning("RSS feed fetch failed: %s", url)
        return 0, 0
    feed = feedparser.parse(raw)
    entries = feed.entries or []
    first_run = src["last_seen"] is None
    # The date floor (when set) is the authoritative first-run gate; otherwise
    # fall back to the most-recent-N limit to avoid backfilling.
    if first_run and since_ts is None:
        entries = entries[:first_run_limit]

    new = 0
    skipped = 0
    fetched = 0  # full-page fetches done this run (budget-limited)
    for entry in entries:
        link = entry.get("link")
        if not link:
            continue
        # Global ingest floor: drop entries dated before the cutoff (cheap check
        # before fetching/extracting the article).
        if since_ts is not None:
            ets = _entry_ts(entry)
            if ets is not None and ets < since_ts:
                skipped += 1
                continue
        from .store import url_hash

        if store.web_item_exists(url_hash(link)):
            continue
        title = entry.get("title", "")
        published = entry.get("published") or entry.get("updated") or ""
        # Bound full-page fetches per feed per run (a date-floored first run can
        # otherwise expand to dozens). Beyond the budget, fall back to the feed's
        # own summary so the item is still ingested + scorable, just without the
        # full article text.
        if fetched < RSS_FETCH_BUDGET:
            text = _extract_article(fetcher, link) or _strip_html(
                entry.get("summary", "")
            )
            fetched += 1
            time.sleep(POLITE_DELAY / 2)
        else:
            text = _strip_html(entry.get("summary", ""))
        item_id = store.insert_web_item(
            url=link,
            source_id=src["id"],
            title=title,
            text_excerpt=text[:4000],
            published_at=published,
        )
        if item_id:
            new += 1

    if fetched >= RSS_FETCH_BUDGET:
        log.info(
            "RSS %s: hit per-run fetch budget (%d); remaining items used feed "
            "summaries", url, RSS_FETCH_BUDGET,
        )
    store.mark_source_run(src["id"], last_seen="seen")
    return new, skipped


def _ingest_site(store: Store, fetcher: WebFetcher, src, max_new: int) -> int:
    base = src["key"]
    html = fetcher.get_text(base)
    if html is None:
        log.warning("site fetch failed: %s", base)
        return 0

    candidates = _candidate_article_links(base, html)
    new = 0
    for link in candidates:
        if new >= max_new:
            break
        from .store import url_hash

        if store.web_item_exists(url_hash(link)):
            continue
        text = _extract_article(fetcher, link)
        if not text or len(text) < 200:
            # Skip pages that don't extract as real articles.
            continue
        title = _first_line(text)
        item_id = store.insert_web_item(
            url=link,
            source_id=src["id"],
            title=title,
            text_excerpt=text[:4000],
            published_at="",
        )
        if item_id:
            new += 1
        time.sleep(POLITE_DELAY)

    store.mark_source_run(src["id"], last_seen="seen")
    return new


def _candidate_article_links(base: str, html: str) -> list[str]:
    parser = _LinkExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML shouldn't crash a run
        pass

    base_host = (urlparse(base).hostname or "").lower()
    seen: set[str] = set()
    out: list[str] = []
    for href in parser.links:
        absolute = urljoin(base, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if (parsed.hostname or "").lower() != base_host:
            continue
        if absolute in seen:
            continue
        if not _looks_like_article(parsed.path):
            continue
        seen.add(absolute)
        out.append(absolute)
    return out


def _looks_like_article(path: str) -> bool:
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    last = segments[-1].lower()
    if any(seg.lower() in _NON_ARTICLE_SEGMENTS for seg in segments):
        return False
    # Heuristic: a slug-like final segment (hyphenated words or numeric id) and
    # reasonable length suggests an article rather than a nav/listing page.
    slug = last.rsplit(".", 1)[0]
    if len(slug) < 12:
        return False
    return ("-" in slug) or any(ch.isdigit() for ch in slug)


def _extract_article(fetcher: WebFetcher, url: str) -> str:
    html = fetcher.get_text(url)
    if not html:
        return ""
    try:
        extracted = trafilatura.extract(
            html, include_comments=False, include_tables=False
        )
    except Exception:  # noqa: BLE001
        extracted = None
    return (extracted or "").strip()


def _strip_html(text: str) -> str:
    class _Stripper(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    s = _Stripper()
    try:
        s.feed(text or "")
    except Exception:  # noqa: BLE001
        return text or ""
    return " ".join("".join(s.parts).split())


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""
