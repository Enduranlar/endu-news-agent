"""Turkish race tracking from the teamrunbo.com race calendar.

The calendar at https://teamrunbo.com/yaristakvimimiz/ is a single 6-column table:
  Yarış Tipi | Yarış Adı (+link) | Tarih | Yer | Parkur Mesafeleri | Notlar
Turkish races are flagged with 🇹🇷 in Notlar and/or a Turkish location/host.

On each daily run we:
  1. Extract every race row and upsert TR races into the `races` table.
  2. Emit a one-time "upcoming race" feed item (category upcoming_races_tr) for
     newly-seen TR races starting within the horizon.
  3. For TR races that have recently finished, fetch the linked race page and ask
     the LLM to extract finishing results (top finishers) — emitting a one-time
     feed item (category race_results) when results are found.

Feed items are written as pre-scored `web_items` (relevant=1, category set) so the
existing report flow picks them up with zero report changes, and the LLM relevance
step skips them.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import trafilatura

from .config_loader import Interests
from .ingest_web import POLITE_DELAY, WebFetcher
from .llm import LLMClient
from .store import Store

log = logging.getLogger("agent.races")

CALENDAR_URL = "https://teamrunbo.com/yaristakvimimiz/"

# Report shows upcoming TR races within this horizon (a live calendar snapshot,
# rendered each issue — see report.py). Results are chased per-race as one-time
# feed items.
REPORT_UPCOMING_DAYS = 45
REPORT_UPCOMING_LIMIT = 15

RESULTS_LOOKBACK_DAYS = 21   # only chase results for races finished this recently
RESULTS_MAX_PER_RUN = 5      # cap result-page fetches per run (network + LLM)
RESULTS_IMPORTANCE = 4

CATEGORY_RESULTS = "race_results"

# Turkish provinces (ascii-folded) — a reliable positive signal that the "Yer"
# column names a Turkish location.
_TR_PROVINCES = {
    "adana", "adiyaman", "afyon", "afyonkarahisar", "agri", "aksaray", "amasya",
    "ankara", "antalya", "ardahan", "artvin", "aydin", "balikesir", "bartin",
    "batman", "bayburt", "bilecik", "bingol", "bitlis", "bolu", "burdur", "bursa",
    "canakkale", "cankiri", "corum", "denizli", "diyarbakir", "duzce", "edirne",
    "elazig", "erzincan", "erzurum", "eskisehir", "gaziantep", "giresun",
    "gumushane", "hakkari", "hatay", "igdir", "isparta", "istanbul", "izmir",
    "kahramanmaras", "maras", "karabuk", "karaman", "kars", "kastamonu",
    "kayseri", "kilis", "kirikkale", "kirklareli", "kirsehir", "kocaeli",
    "konya", "kutahya", "malatya", "manisa", "mardin", "mersin", "mugla", "mus",
    "nevsehir", "nigde", "ordu", "osmaniye", "rize", "sakarya", "samsun",
    "sanliurfa", "urfa", "siirt", "sinop", "sivas", "sirnak", "tekirdag", "tokat",
    "trabzon", "tunceli", "usak", "van", "yalova", "yozgat", "zonguldak",
    # common race locales that imply Turkey
    "kapadokya", "kackar", "uludag", "abant", "salda", "frigya", "likya",
    "gokceada", "bozcaada", "datca", "fethiye", "oludeniz", "assos", "gelibolu",
}

_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇâÂîÎûÛ", "iissgguuooccaaiiuu")


def _fold(text: str) -> str:
    return (text or "").translate(_FOLD).lower()


@dataclass
class Race:
    race_key: str
    name: str
    url: str
    location: str
    distances: str
    race_type: str
    notes: str
    date_raw: str
    start_date: Optional[date]
    end_date: Optional[date]
    is_tr: bool


@dataclass
class RaceTrackResult:
    new_races: int = 0
    results_found: int = 0
    results_attempted: int = 0
    total_tr: int = 0
    fetch_ok: bool = True


# --- date parsing ------------------------------------------------------------

_RE_CROSS = re.compile(r"^(\d{1,2})\.(\d{1,2})-(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_RE_SAME = re.compile(r"^(\d{1,2})-(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_RE_SINGLE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _mk(d: int, m: int, y: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_dates(raw: str) -> tuple[Optional[date], Optional[date]]:
    """Parse the Tarih cell into (start, end). Handles single dates and ranges
    within a month or across months. Returns (None, None) if undatable (TBD)."""
    s = re.sub(r"\s+", "", raw or "")
    m = _RE_CROSS.match(s)
    if m:
        d1, m1, d2, m2, y = (int(x) for x in m.groups())
        return _mk(d1, m1, y), _mk(d2, m2, y)
    m = _RE_SAME.match(s)
    if m:
        d1, d2, mo, y = (int(x) for x in m.groups())
        return _mk(d1, mo, y), _mk(d2, mo, y)
    m = _RE_SINGLE.search(s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        dt = _mk(d, mo, y)
        return dt, dt
    return None, None


# --- TR detection ------------------------------------------------------------


def is_turkish_race(location: str, notes: str, url: str) -> bool:
    if "🇹🇷" in (notes or ""):
        return True
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(".tr") or "apphurra" in host:
        return True
    loc_tokens = set(re.split(r"[^a-z]+", _fold(location)))
    return bool(loc_tokens & _TR_PROVINCES)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _fold(name)).strip("-")[:80]


# --- table parsing -----------------------------------------------------------


class _CalendarParser(HTMLParser):
    """Collects table rows as lists of (cell_text, cell_href)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[tuple[str, Optional[str]]]] = []
        self._cur: Optional[list] = None
        self._cell: Optional[list] = None
        self._cell_href: Optional[str] = None
        self._in_table = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "table":
            self._in_table += 1
        elif tag == "tr" and self._in_table:
            self._cur = []
        elif tag in ("td", "th") and self._cur is not None:
            self._cell = []
            self._cell_href = None
        elif tag == "a" and self._cell is not None and d.get("href"):
            if not self._cell_href:
                self._cell_href = d["href"]

    def handle_endtag(self, tag):
        if tag == "table" and self._in_table:
            self._in_table -= 1
        elif tag == "tr" and self._cur is not None:
            if self._cur:
                self.rows.append(self._cur)
            self._cur = None
        elif tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self._cur.append((text, self._cell_href))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def extract_races(html: str) -> list[Race]:
    parser = _CalendarParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML shouldn't crash a run
        pass

    races: list[Race] = []
    seen_keys: set[str] = set()
    for row in parser.rows:
        if len(row) < 5:
            continue
        name = row[1][0].strip()
        date_raw = row[2][0].strip()
        start, end = parse_dates(date_raw)
        # A real data row has a name and a parseable date; this skips the column
        # header and the year/month section rows.
        if not name or (start is None and end is None):
            continue
        url = (row[1][1] or "").strip()
        location = row[3][0].strip()
        distances = row[4][0].strip()
        race_type = row[0][0].strip()
        notes = row[5][0].strip() if len(row) > 5 else ""

        key = f"{_slug(name)}|{re.sub(r'[^0-9.-]', '', date_raw)}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        races.append(
            Race(
                race_key=key,
                name=name,
                url=url,
                location=location,
                distances=distances,
                race_type=race_type,
                notes=notes,
                date_raw=date_raw,
                start_date=start,
                end_date=end,
                is_tr=is_turkish_race(location, notes, url),
            )
        )
    return races


# --- tracking ----------------------------------------------------------------


def _emit_feed_item(
    store: Store,
    url: str,
    title: str,
    one_line: str,
    category: str,
    importance: int,
    published_at: str,
) -> bool:
    item_id = store.insert_web_item(
        url=url,
        source_id=None,
        title=title,
        text_excerpt=one_line,
        published_at=published_at,
    )
    if item_id is None:
        return False  # already emitted (dedup by url hash)
    store.update_web_score(item_id, True, category, importance, one_line)
    return True


def run_race_tracking(
    store: Store,
    fetcher: WebFetcher,
    llm: LLMClient,
    interests: Interests,
    today: date,
    since_floor: Optional[date] = None,
    calendar_html: Optional[str] = None,
) -> RaceTrackResult:
    """Track TR races. `calendar_html` lets the caller fetch the calendar once and
    reuse it across agents (the LLM results extraction still runs per agent)."""
    result = RaceTrackResult()

    html = calendar_html if calendar_html is not None else fetcher.get_text(CALENDAR_URL)
    if not html:
        log.warning("race calendar fetch failed: %s", CALENDAR_URL)
        result.fetch_ok = False
        return result

    races = extract_races(html)
    log.info("race calendar: %d rows parsed", len(races))

    # 1. Upsert TR races, refreshing status from the date.
    for race in races:
        if not race.is_tr:
            continue
        result.total_tr += 1
        status = (
            "completed"
            if race.end_date is not None and race.end_date < today
            else "upcoming"
        )
        is_new = store.upsert_race(
            race_key=race.race_key,
            name=race.name,
            url=race.url,
            location=race.location,
            distances=race.distances,
            race_type=race.race_type,
            notes=race.notes,
            date_raw=race.date_raw,
            start_date=race.start_date.isoformat() if race.start_date else None,
            end_date=race.end_date.isoformat() if race.end_date else None,
            is_tr=True,
            status=status,
        )
        if is_new:
            result.new_races += 1

    # 2. Chase results for recently-finished TR races (capped, budget-friendly).
    #    Upcoming races are rendered live by the report from the races table (no
    #    per-race feed items) to avoid flooding.
    lookback_floor = today - timedelta(days=RESULTS_LOOKBACK_DAYS)
    if since_floor and since_floor > lookback_floor:
        lookback_floor = since_floor
    candidates = store.tr_races_for_results(
        lookback_floor.isoformat(), today.isoformat()
    )
    for r in candidates[:RESULTS_MAX_PER_RUN]:
        result.results_attempted += 1
        page = fetcher.get_text(r["url"])
        page_text = ""
        if page:
            try:
                page_text = trafilatura.extract(
                    page, include_comments=False, include_tables=True
                ) or ""
            except Exception:  # noqa: BLE001
                page_text = ""
        if not page_text:
            continue  # page unreachable / empty — retry on a later run
        extract = llm.extract_race_results(r["name"], page_text)
        if extract and extract.found and extract.summary:
            _emit_feed_item(
                store,
                _feed_url_from_row(r, "sonuc"),
                r["name"],
                extract.summary,
                CATEGORY_RESULTS,
                RESULTS_IMPORTANCE,
                r["end_date"] or "",
            )
            store.mark_race_results(r["race_key"], extract.summary)
            result.results_found += 1
        time.sleep(POLITE_DELAY)

    log.info(
        "races: %d TR (%d new), %d results found / %d attempted",
        result.total_tr,
        result.new_races,
        result.results_found,
        result.results_attempted,
    )
    return result


def _feed_url_from_row(row, kind: str) -> str:
    base = (row["url"] or "").strip() or CALENDAR_URL
    return f"{base}#runbo-{kind}-{row['race_key']}"
