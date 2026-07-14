"""Parse the operator-curated source files and the interest list.

These files are hand-edited by a non-developer, so parsing is deliberately
tolerant: blank lines and `#` comments are ignored, whitespace is trimmed.
The config files are the source of truth; `store.reconcile_sources` syncs them
into the DB on each run. This module also appends approved suggestions back to
the config files (the `approve` workflow).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import settings


@dataclass(frozen=True)
class IGAccount:
    handle: str
    note: str = ""


@dataclass(frozen=True)
class WebSource:
    kind: str  # "rss" | "site"
    url: str
    note: str = ""


@dataclass(frozen=True)
class Category:
    id: str
    label: str


@dataclass(frozen=True)
class Interests:
    categories: list[Category]
    context: str

    @property
    def category_ids(self) -> list[str]:
        return [c.id for c in self.categories]

    def label_for(self, category_id: str) -> str:
        for c in self.categories:
            if c.id == category_id:
                return c.label
        return category_id


def _iter_meaningful_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _normalize_handle(handle: str) -> str:
    """Strip leading @, surrounding URL bits, and lowercase."""
    h = handle.strip().lstrip("@")
    # Tolerate someone pasting a full instagram URL.
    if "instagram.com/" in h:
        h = h.split("instagram.com/", 1)[1]
    h = h.strip("/").split("/")[0].split("?")[0]
    return h.lower()


def load_ig_accounts(path: Path | None = None) -> list[IGAccount]:
    path = path or settings.IG_ACCOUNTS_FILE
    accounts: list[IGAccount] = []
    seen: set[str] = set()
    for line in _iter_meaningful_lines(path):
        parts = line.split("|", 1)
        handle = _normalize_handle(parts[0])
        note = parts[1].strip() if len(parts) > 1 else ""
        if not handle or handle in seen:
            continue
        seen.add(handle)
        accounts.append(IGAccount(handle=handle, note=note))
    return accounts


def load_web_sources(path: Path | None = None) -> list[WebSource]:
    path = path or settings.WEBSITES_FILE
    sources: list[WebSource] = []
    seen: set[str] = set()
    for line in _iter_meaningful_lines(path):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            # Be lenient: bare URL with no type → assume rss.
            kind, url, note = "rss", parts[0], ""
        else:
            kind = parts[0].lower()
            url = parts[1]
            note = parts[2] if len(parts) > 2 else ""
        if kind not in ("rss", "site"):
            kind = "rss"
        url = url.rstrip("/") if url.endswith("/") and url.count("/") > 2 else url
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(WebSource(kind=kind, url=url, note=note))
    return sources


def load_interests(path: Path | None = None) -> Interests:
    path = path or settings.INTERESTS_FILE
    if not path.exists():
        raise settings.ConfigError(f"interests file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cats_raw = data.get("categories", []) or []
    categories = [
        Category(id=str(c["id"]).strip(), label=str(c.get("label", c["id"])).strip())
        for c in cats_raw
        if c.get("id")
    ]
    if not categories:
        raise settings.ConfigError("interests.yaml must define at least one category")
    context = str(data.get("context", "")).strip()
    return Interests(categories=categories, context=context)


# --- Append helpers for the approve workflow ---------------------------------


def append_ig_account(handle: str, note: str = "", path: Path | None = None) -> bool:
    """Append a handle to igaccounts.md if not already present. Returns True if added."""
    path = path or settings.IG_ACCOUNTS_FILE
    handle = _normalize_handle(handle)
    existing = {a.handle for a in load_ig_accounts(path)}
    if handle in existing:
        return False
    line = f"{handle}" + (f" | {note}" if note else "")
    _append_line(path, line)
    return True


def append_web_source(
    kind: str, url: str, note: str = "", path: Path | None = None
) -> bool:
    """Append a site/rss source to websites.md if not already present."""
    path = path or settings.WEBSITES_FILE
    kind = kind.lower()
    if kind not in ("rss", "site"):
        raise ValueError(f"web source kind must be 'rss' or 'site', got {kind!r}")
    existing = {s.url for s in load_web_sources(path)}
    if url in existing:
        return False
    line = f"{kind} | {url}" + (f" | {note}" if note else "")
    _append_line(path, line)
    return True


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if existing.endswith("\n") or not existing else "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{sep}{line}\n")
