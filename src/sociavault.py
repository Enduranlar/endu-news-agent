"""SociaVault API client: Instagram profiles (posts + related accounts) and
Google search for source discovery.

- Base URL: https://api.sociavault.com
- Auth: X-API-Key header on every request.
- SociaVault is a global API that scrapes Instagram server-side, so our request's
  exit IP is irrelevant to the scrape — calls ALWAYS go direct. The Turkish
  residential proxy is reserved for direct `.tr` web fetches in ingest_web.
- Exponential backoff on 429/5xx; per-call credit accounting via CreditTracker.

IMPORTANT: response shapes are deeply nested and were confirmed against the live
API / docs (https://docs.sociavault.com/llms.txt). Parsing walks several candidate
paths defensively so a minor shape change doesn't wipe a run. Run
`python -m src.main test-sociavault --handle <h>` after deploy to confirm field
paths against a live call (Milestone 1).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .store import Store
from .timeutil import istanbul_day

log = logging.getLogger("agent.sociavault")

BASE_URL = "https://api.sociavault.com"
PROFILE_ENDPOINT = "/v1/scrape/instagram/profile"
SEARCH_ENDPOINT = "/v1/scrape/google/search"

PROFILE_CREDIT_COST = 1  # documented cost of the trimmed profile call


class SociaVaultError(RuntimeError):
    """Non-recoverable SociaVault API error."""


class CreditBudgetExceeded(RuntimeError):
    """Raised when a call would exceed the daily credit budget."""


class CreditTracker:
    """Tracks SociaVault credit spend against the daily budget (store-backed).

    The guardrail degrades gracefully: callers check `would_exceed()` before
    optional (discovery) calls and skip them rather than crashing the run.
    """

    def __init__(self, store: Store, budget: int, day: Optional[str] = None):
        self.store = store
        self.budget = budget
        self.day = day or istanbul_day()

    def spent(self) -> int:
        return self.store.credits_spent_today(self.day)

    def remaining(self) -> int:
        return max(0, self.budget - self.spent())

    def would_exceed(self, cost: int) -> bool:
        return self.spent() + cost > self.budget

    def charge(self, credits: int, calls: int = 1) -> None:
        self.store.record_credits(self.day, credits, calls)


@dataclass
class ParsedPost:
    shortcode: str
    url: str
    caption: str = ""
    taken_at_timestamp: Optional[int] = None
    is_video: bool = False
    likes: int = 0
    comments: int = 0


@dataclass
class RelatedAccount:
    handle: str
    full_name: str = ""


@dataclass
class ParsedProfile:
    username: str
    full_name: str = ""
    biography: str = ""
    followers: int = 0
    is_verified: bool = False
    business_category: str = ""
    bio_links: list[str] = field(default_factory=list)
    posts: list[ParsedPost] = field(default_factory=list)
    related_accounts: list[RelatedAccount] = field(default_factory=list)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


def _deep_get(obj: Any, *paths: str) -> Any:
    """Return the first non-None value found at any dotted path in `paths`.

    Numeric path segments index either dicts (SociaVault's "array-like" objects,
    e.g. {"0": ...}) or real JSON lists.
    """
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def _coerce_list(value: Any) -> list:
    """Normalise SociaVault's "array-like" dicts into a real list.

    SociaVault frequently returns collections as index-keyed objects
    ({"0": {...}, "1": {...}}) rather than JSON arrays. This returns the ordered
    values for those, passes real lists through, and yields [] for anything else.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        keys = list(value.keys())
        if keys and all(isinstance(k, str) and k.isdigit() for k in keys):
            return [value[k] for k in sorted(keys, key=int)]
        return list(value.values())
    return []


def _as_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


class SociaVaultClient:
    def __init__(
        self,
        api_key: str,
        tracker: Optional[CreditTracker] = None,
        timeout: float = 45.0,
        max_retries: int = 4,
    ):
        self.api_key = api_key
        self.tracker = tracker
        self.max_retries = max_retries
        # SociaVault is a global API (it scrapes Instagram server-side), so our
        # request IP is irrelevant to the scrape — calls always go direct.
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SociaVaultClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- HTTP with backoff + credit accounting -------------------------

    def _request(
        self, path: str, params: dict[str, Any], cost: int
    ) -> dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                self._charge(resp, cost)
                try:
                    return resp.json()
                except ValueError as exc:
                    raise SociaVaultError(
                        f"Non-JSON 200 response from {path}"
                    ) from exc

            if resp.status_code in (429, 500, 502, 503, 504):
                log.warning(
                    "SociaVault %s -> %s (attempt %d/%d)",
                    path,
                    resp.status_code,
                    attempt + 1,
                    self.max_retries,
                )
                retry_after = resp.headers.get("retry-after")
                self._sleep_backoff(attempt, retry_after)
                last_exc = SociaVaultError(
                    f"{resp.status_code} from {path}: {resp.text[:200]}"
                )
                continue

            # Non-retryable (4xx other than 429).
            raise SociaVaultError(
                f"{resp.status_code} from {path}: {resp.text[:300]}"
            )

        raise SociaVaultError(
            f"SociaVault request to {path} failed after {self.max_retries} attempts: "
            f"{last_exc}"
        )

    def _charge(self, resp: httpx.Response, fallback_cost: int) -> None:
        if self.tracker is None:
            return
        # Prefer a credits-spent header if the API exposes one; else estimate.
        header_cost = None
        for h in ("x-credits-used", "x-credit-cost", "x-api-credits"):
            if h in resp.headers:
                header_cost = _as_int(resp.headers[h], fallback_cost)
                break
        self.tracker.charge(header_cost if header_cost is not None else fallback_cost)

    @staticmethod
    def _sleep_backoff(attempt: int, retry_after: Optional[str] = None) -> None:
        if retry_after:
            try:
                time.sleep(min(60.0, float(retry_after)))
                return
            except ValueError:
                pass
        time.sleep(min(30.0, 1.5 * (2 ** attempt)))

    # --- Instagram profile ---------------------------------------------

    def get_instagram_profile(
        self, handle: str, trim: bool = True
    ) -> ParsedProfile:
        """Fetch profile + recent posts + related accounts (1 credit).

        If related accounts are absent under trim=true, retry once without trim.
        """
        if self.tracker and self.tracker.would_exceed(PROFILE_CREDIT_COST):
            raise CreditBudgetExceeded(
                f"profile call for {handle} would exceed daily credit budget"
            )
        raw = self._request(
            PROFILE_ENDPOINT,
            {"handle": handle, "trim": "true" if trim else "false"},
            cost=PROFILE_CREDIT_COST,
        )
        profile = self._parse_profile(raw, handle)

        if trim and not profile.related_accounts:
            # Related profiles can be dropped under trim=true; try once without.
            if not (self.tracker and self.tracker.would_exceed(PROFILE_CREDIT_COST)):
                try:
                    raw2 = self._request(
                        PROFILE_ENDPOINT,
                        {"handle": handle, "trim": "false"},
                        cost=PROFILE_CREDIT_COST,
                    )
                    full = self._parse_profile(raw2, handle)
                    if full.related_accounts:
                        profile.related_accounts = full.related_accounts
                    if len(full.posts) > len(profile.posts):
                        profile.posts = full.posts
                except SociaVaultError as exc:
                    log.debug("untrimmed retry for %s failed: %s", handle, exc)
        return profile

    def fetch_profile_raw(self, handle: str, trim: bool = True) -> dict[str, Any]:
        """Return the raw profile JSON (no parsing) — for inspecting field paths."""
        return self._request(
            PROFILE_ENDPOINT,
            {"handle": handle, "trim": "true" if trim else "false"},
            cost=PROFILE_CREDIT_COST,
        )

    def _parse_profile(self, raw: dict, handle: str) -> ParsedProfile:
        user = _deep_get(raw, "data.data.user", "data.user", "user", "data") or {}
        if not isinstance(user, dict):
            user = {}

        username = (
            _deep_get(user, "username") or handle
        )
        followers = _as_int(
            _deep_get(user, "edge_followed_by.count", "follower_count", "followers")
        )
        # bio_links is an array-like dict ({"0": {...}}); coerce before iterating.
        bio_links = [
            l.get("url", "")
            for l in _coerce_list(_deep_get(user, "bio_links"))
            if isinstance(l, dict) and l.get("url")
        ]

        posts = self._parse_posts(user, raw)
        related = self._parse_related(user, raw)

        return ParsedProfile(
            username=str(username),
            full_name=str(_deep_get(user, "full_name") or ""),
            biography=str(_deep_get(user, "biography") or ""),
            followers=followers,
            is_verified=bool(_deep_get(user, "is_verified")),
            business_category=str(
                _deep_get(user, "business_category_name", "category_name") or ""
            ),
            bio_links=bio_links,
            posts=posts,
            related_accounts=related,
        )

    # Containers that may hold recent posts. SociaVault returns BOTH an image
    # timeline and a reels/video timeline, each as an array-like dict, so we
    # gather every non-empty container and dedup by shortcode rather than picking
    # one. Add a path here if a live --raw dump shows posts somewhere new.
    _POST_PATHS_IN_USER = (
        "edge_owner_to_timeline_media.edges",
        "edge_felix_video_timeline.edges",
        "timeline_media.edges",
        "posts",
        "recent_posts",
        "media",
        "items",
    )
    _POST_PATHS_IN_RAW = (
        "data.data.posts",
        "data.posts",
        "data.data.recent_posts",
        "data.recent_posts",
        "data.data.media",
        "data.media",
        "data.data.items",
        "data.items",
        "posts",
        "recent_posts",
        "media",
    )

    def _parse_posts(self, user: dict, raw: dict) -> list[ParsedPost]:
        containers: list = []
        for path in self._POST_PATHS_IN_USER:
            v = _deep_get(user, path)
            if v:
                containers.append(v)
        for path in self._POST_PATHS_IN_RAW:
            v = _deep_get(raw, path)
            if v:
                containers.append(v)

        posts: list[ParsedPost] = []
        seen: set[str] = set()
        for container in containers:
            for edge in _coerce_list(container):
                if not isinstance(edge, dict):
                    continue
                # Each entry is either {"node": {...}} or a flat post object.
                node = edge["node"] if isinstance(edge.get("node"), dict) else edge
                shortcode = _deep_get(node, "shortcode", "code")
                if not shortcode or str(shortcode) in seen:
                    continue
                seen.add(str(shortcode))
                caption = (
                    _deep_get(node, "edge_media_to_caption.edges.0.node.text")
                    or _deep_get(node, "caption.text", "caption")
                    or ""
                )
                typename = str(_deep_get(node, "__typename") or "")
                posts.append(
                    ParsedPost(
                        shortcode=str(shortcode),
                        url=f"https://www.instagram.com/p/{shortcode}/",
                        caption=str(caption),
                        taken_at_timestamp=_as_int(
                            _deep_get(node, "taken_at_timestamp", "taken_at"), 0
                        )
                        or None,
                        is_video=bool(_deep_get(node, "is_video"))
                        or "Video" in typename,
                        likes=_as_int(
                            _deep_get(node, "edge_liked_by.count", "like_count", "likes")
                        ),
                        comments=_as_int(
                            _deep_get(
                                node,
                                "edge_media_to_comment.count",
                                "comment_count",
                                "comments",
                            )
                        ),
                    )
                )
        return posts

    def _parse_related(self, user: dict, raw: dict) -> list[RelatedAccount]:
        edges = (
            _deep_get(
                user,
                "edge_related_profiles.edges",
            )
            or _deep_get(raw, "data.data.related_profiles", "data.related_profiles")
            or []
        )
        out: list[RelatedAccount] = []
        seen: set[str] = set()
        for edge in _coerce_list(edges):
            node = edge.get("node") if isinstance(edge, dict) else edge
            if not isinstance(node, dict):
                continue
            uname = _deep_get(node, "username")
            if not uname:
                continue
            uname = str(uname).lower()
            if uname in seen:
                continue
            seen.add(uname)
            out.append(
                RelatedAccount(
                    handle=uname,
                    full_name=str(_deep_get(node, "full_name") or ""),
                )
            )
        return out

    # --- Google search (discovery) -------------------------------------

    def google_search(self, query: str, cost: int = 1) -> list[SearchResult]:
        if self.tracker and self.tracker.would_exceed(cost):
            raise CreditBudgetExceeded("google_search would exceed daily credit budget")
        raw = self._request(SEARCH_ENDPOINT, {"query": query}, cost=cost)
        results_raw = (
            _deep_get(
                raw,
                "data.data.organic_results",
                "data.organic_results",
                "data.results",
                "results",
                "organic_results",
            )
            or []
        )
        out: list[SearchResult] = []
        for r in _coerce_list(results_raw):
            if not isinstance(r, dict):
                continue
            url = _deep_get(r, "link", "url")
            if not url:
                continue
            out.append(
                SearchResult(
                    title=str(_deep_get(r, "title") or ""),
                    url=str(url),
                    snippet=str(_deep_get(r, "snippet", "description") or ""),
                )
            )
        return out
