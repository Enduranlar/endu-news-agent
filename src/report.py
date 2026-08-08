"""Build the twice-weekly summary report (Markdown + inline-CSS HTML).

Pulls relevant items ingested since the last sent report, groups them by interest
category (interests.yaml order), sorts by importance then recency, and asks the
summary model to write a 2–3 sentence "what matters this week" intro. Per-category
bullets are rendered deterministically from the stored one_line + link + date so
links are never hallucinated. (Source suggestions are reviewed in the web admin
UI, not the report.)
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config_loader import Interests
from .llm import LLMClient
from .races import REPORT_UPCOMING_DAYS, REPORT_UPCOMING_LIMIT
from .store import Store
from .timeutil import (
    PERIOD_TR,
    istanbul_long_date_tr,
    istanbul_now,
    istanbul_short_date_tr,
)

log = logging.getLogger("agent.report")


@dataclass
class ReportItem:
    category: str
    one_line: str
    url: str
    date: str
    source: str
    importance: int


@dataclass
class ReportBundle:
    period: str
    title: str
    date_str: str
    intro: str
    groups: list[tuple[str, list[ReportItem]]]  # (category label, items)
    upcoming_races: list[dict]
    item_count: int
    categories_covered: list[str] = field(default_factory=list)
    markdown: str = ""
    html: str = ""


def _ts_to_date(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def build_report(
    store: Store, llm: LLMClient, interests: Interests, period: str
) -> ReportBundle:
    since = store.last_report_sent_at()
    ig_rows, web_rows = store.relevant_items_since(since)

    items: list[ReportItem] = []
    for r in ig_rows:
        items.append(
            ReportItem(
                category=r["category"] or "major_news",
                one_line=r["one_line"] or (r["caption"] or "")[:140],
                url=r["url"],
                date=_ts_to_date(r["ts"]),
                source=f"IG @{r['handle']}",
                importance=r["importance"] or 1,
            )
        )
    for r in web_rows:
        items.append(
            ReportItem(
                category=r["category"] or "major_news",
                one_line=r["one_line"] or (r["title"] or "")[:140],
                url=r["url"],
                date=(r["published_at"] or "")[:10],
                source=r["title"] or _host(r["url"]),
                importance=r["importance"] or 1,
            )
        )

    # Report-wide de-duplication: the same story often arrives from several
    # sources (different IG accounts / sites) and survives exact dedup, so an LLM
    # pass clusters items describing the SAME event across ALL categories and we
    # keep the single best one per cluster.
    items, removed = _dedupe_report_items(items, llm)
    if removed:
        log.info("report dedup: removed %d duplicate item(s)", removed)

    # Group by interest-list category order; sort within by importance then recency.
    groups: list[tuple[str, list[ReportItem]]] = []
    covered: list[str] = []
    for cat in interests.categories:
        bucket = [it for it in items if it.category == cat.id]
        if not bucket:
            continue
        bucket.sort(key=lambda it: (it.importance, it.date), reverse=True)
        groups.append((cat.label, bucket))
        covered.append(cat.id)

    now = istanbul_now()
    today = now.date()
    horizon = today + timedelta(days=REPORT_UPCOMING_DAYS)
    upcoming_races = [
        dict(r)
        for r in store.upcoming_tr_races(
            today.isoformat(), horizon.isoformat(), REPORT_UPCOMING_LIMIT
        )
    ]

    date_str = istanbul_long_date_tr(now)
    period_label = PERIOD_TR.get(period, period.capitalize())
    title = (
        f"Dayanıklılık Sporları Haber Özeti — {period_label} brifingi "
        f"({istanbul_short_date_tr(now)})"
    )

    intro = _write_intro(llm, period, groups)

    bundle = ReportBundle(
        period=period,
        title=title,
        date_str=date_str,
        intro=intro,
        groups=groups,
        upcoming_races=upcoming_races,
        item_count=len(items),
        categories_covered=covered,
    )
    bundle.markdown = _render_markdown(bundle)
    bundle.html = _render_html(bundle)
    return bundle


def duplicate_drop_indices(entries: list[dict], llm: LLMClient) -> set[int]:
    """Return the set of entry indices to drop as duplicates, report-wide.

    `entries` is a list of dicts with keys: category, source, one_line, importance,
    date. Asks the LLM to cluster entries describing the same underlying event
    (across all categories), merges overlapping/chained clusters (union-find), and
    keeps the single best per cluster — highest importance, then most recent, then
    earliest listed. Returns an empty set on any LLM failure (never raises). Shared
    by live report building and offline report reprocessing."""
    n = len(entries)
    if n < 2:
        return set()

    payload = [
        {
            "index": i,
            "category": e.get("category", ""),
            "source": e.get("source", ""),
            "one_line": e.get("one_line", ""),
        }
        for i, e in enumerate(entries)
    ]
    groups = llm.find_duplicate_groups(payload)
    if not groups:
        return set()

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for g in groups:
        idxs = [i for i in g if isinstance(i, int) and 0 <= i < n]
        for j in idxs[1:]:
            ra, rb = find(idxs[0]), find(j)
            if ra != rb:
                parent[ra] = rb

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    drop: set[int] = set()
    for members in clusters.values():
        if len(members) < 2:
            continue
        keep = max(
            members,
            key=lambda i: (
                entries[i].get("importance", 1),
                entries[i].get("date", "") or "",
                -i,
            ),
        )
        drop.update(m for m in members if m != keep)
    return drop


def _dedupe_report_items(
    items: list[ReportItem], llm: LLMClient
) -> tuple[list[ReportItem], int]:
    """Drop ReportItems that duplicate the same story, report-wide (see
    duplicate_drop_indices). Returns (kept_items, removed_count)."""
    if len(items) < 2:
        return items, 0
    entries = [
        {
            "category": it.category,
            "source": it.source,
            "one_line": it.one_line,
            "importance": it.importance,
            "date": it.date,
        }
        for it in items
    ]
    drop = duplicate_drop_indices(entries, llm)
    if not drop:
        return items, 0
    kept = [it for k, it in enumerate(items) if k not in drop]
    return kept, len(drop)


def _write_intro(
    llm: LLMClient, period: str, groups: list[tuple[str, list[ReportItem]]]
) -> str:
    if not groups:
        return "Son özetten bu yana yeni alakalı bir gelişme yok."
    lines = []
    for label, items in groups:
        for it in items[:6]:
            lines.append(f"- [{label}] ({it.importance}/5) {it.one_line}")
    digest = "\n".join(lines)
    prompt = (
        "Bir dayanıklılık sporları beslenmesi e-ticaret kurucusu için rapor "
        "giriş özetini yazıyorsun. Aşağıda bu dönemin alakalı haber öğeleri, "
        "kategoriye göre gruplanmış ve önem puanıyla birlikte verilmiştir. Kısa, "
        "2-3 cümlelik bir 'bu hafta öne çıkanlar' girişi yaz: öz, taranabilir, "
        "dolgu yok, giriş cümlesi yok, başlık yok. En önemli gelişmeyle başla. "
        "TÜRKÇE yaz.\n\n"
        f"ÖĞELER:\n{digest}\n\nSadece 2-3 cümlelik özeti yaz."
    )
    try:
        text = llm.write_summary(prompt)
        return text or "Son özetten bu yana öne çıkan gelişmeler."
    except Exception as exc:  # noqa: BLE001 — never fail a report over the intro
        log.warning("intro generation failed (%s); using fallback", exc)
        return "Son özetten bu yana öne çıkan gelişmeler."


def _host(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or url
    return host[4:] if host.startswith("www.") else host


# --- Markdown rendering ------------------------------------------------------


def _render_markdown(b: ReportBundle) -> str:
    out: list[str] = []
    out.append(f"# {b.title}")
    out.append("")
    out.append(f"*{b.date_str}*")
    out.append("")
    out.append("## Bu hafta öne çıkanlar")
    out.append("")
    out.append(b.intro)
    out.append("")

    if not b.groups:
        out.append("_Bu dönemde yeni alakalı öğe yok._")
        out.append("")
    for label, items in b.groups:
        out.append(f"## {label}")
        out.append("")
        for it in items:
            date = f" — {it.date}" if it.date else ""
            out.append(f"- **[{it.importance}/5]** {it.one_line} "
                       f"([{it.source}]({it.url})){date}")
        out.append("")

    if b.upcoming_races:
        out.append(
            f"## Yaklaşan Türkiye yarışları (önümüzdeki {REPORT_UPCOMING_DAYS} gün)"
        )
        out.append("")
        for r in b.upcoming_races:
            loc = f" — {r['location']}" if r["location"] else ""
            dist = f" _{r['distances']}_" if r["distances"] else ""
            link = f" ([site]({r['url']}))" if r["url"] else ""
            out.append(f"- **{r['date_raw']}** {r['name']}{loc}{dist}{link}")
        out.append("")

    return "\n".join(out)


# --- HTML rendering (inline CSS for email clients) ---------------------------


def _esc(text: str) -> str:
    return html.escape(text or "")


def _render_html(b: ReportBundle) -> str:
    body: list[str] = []
    body.append(
        f'<h1 style="font-size:20px;margin:0 0 4px 0;color:#1a1a1a;">{_esc(b.title)}</h1>'
    )
    body.append(
        f'<p style="color:#777;margin:0 0 16px 0;font-size:13px;">{_esc(b.date_str)}</p>'
    )
    body.append(
        '<h2 style="font-size:15px;color:#0b5;border-bottom:1px solid #eee;'
        'padding-bottom:4px;">Bu hafta öne çıkanlar</h2>'
    )
    body.append(
        f'<p style="font-size:14px;line-height:1.5;color:#222;">{_esc(b.intro)}</p>'
    )

    if not b.groups:
        body.append(
            '<p style="color:#777;font-style:italic;">Bu dönemde yeni alakalı '
            "öğe yok.</p>"
        )
    for label, items in b.groups:
        body.append(
            f'<h2 style="font-size:15px;color:#1a1a1a;border-bottom:1px solid #eee;'
            f'padding-bottom:4px;margin-top:20px;">{_esc(label)}</h2>'
        )
        body.append('<ul style="padding-left:18px;margin:8px 0;">')
        for it in items:
            date = f' &mdash; <span style="color:#999;">{_esc(it.date)}</span>' if it.date else ""
            body.append(
                f'<li style="font-size:14px;line-height:1.5;margin-bottom:6px;color:#222;">'
                f'<strong style="color:#0b5;">[{it.importance}/5]</strong> '
                f"{_esc(it.one_line)} "
                f'(<a href="{_esc(it.url)}" style="color:#06c;">{_esc(it.source)}</a>)'
                f"{date}</li>"
            )
        body.append("</ul>")

    if b.upcoming_races:
        body.append(
            f'<h2 style="font-size:15px;color:#06c;border-bottom:1px solid #eee;'
            f'padding-bottom:4px;margin-top:20px;">Yaklaşan Türkiye yarışları '
            f"(önümüzdeki {REPORT_UPCOMING_DAYS} gün)</h2>"
        )
        body.append('<ul style="padding-left:18px;margin:8px 0;">')
        for r in b.upcoming_races:
            loc = f" &mdash; {_esc(r['location'])}" if r["location"] else ""
            dist = (
                f' <span style="color:#999;">{_esc(r["distances"])}</span>'
                if r["distances"]
                else ""
            )
            name = (
                f'<a href="{_esc(r["url"])}" style="color:#06c;">{_esc(r["name"])}</a>'
                if r["url"]
                else _esc(r["name"])
            )
            body.append(
                '<li style="font-size:14px;line-height:1.5;margin-bottom:6px;color:#222;">'
                f'<strong>{_esc(r["date_raw"])}</strong> {name}{loc}{dist}</li>'
            )
        body.append("</ul>")

    inner = "\n".join(body)
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f6f6f6;">'
        '<div style="max-width:640px;margin:0 auto;padding:24px;background:#fff;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        f"{inner}"
        '<p style="margin-top:28px;color:#bbb;font-size:11px;border-top:1px solid '
        '#eee;padding-top:10px;">Bu rapor dayanıklılık haber ajansı tarafından '
        "otomatik olarak oluşturulmuştur.</p>"
        "</div></body></html>"
    )
