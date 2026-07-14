"""SQLite state: sources, ingested items, suggestions, reports, credit log.

This is the single source of truth for dedup and idempotency. Every item is
processed at most once (keyed by IG shortcode / URL hash); reports pull only
items ingested since the last report; the credit guardrail reads/writes here.

Design: plain sqlite3, one connection per process, explicit helper functions.
No ORM — the schema is small and we want it debuggable on a VPS.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import settings
from .config_loader import IGAccount, WebSource

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,              -- 'ig' | 'rss' | 'site'
    key          TEXT NOT NULL,              -- handle (ig) or url (rss/site)
    note         TEXT DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_seen    TEXT,                        -- ts/url cursor (per-source meaning)
    created_at   TEXT NOT NULL,
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS ig_posts (
    post_id      TEXT PRIMARY KEY,            -- shortcode
    handle       TEXT NOT NULL,
    url          TEXT NOT NULL,
    caption      TEXT DEFAULT '',
    ts           INTEGER,                     -- taken_at_timestamp (unix)
    likes        INTEGER DEFAULT 0,
    comments     INTEGER DEFAULT 0,
    relevant     INTEGER,                     -- NULL=unscored, 0/1 after scoring
    category     TEXT,
    importance   INTEGER,
    one_line     TEXT,
    ingested_at  TEXT NOT NULL,
    scored_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ig_posts_ingested ON ig_posts(ingested_at);
CREATE INDEX IF NOT EXISTS idx_ig_posts_handle ON ig_posts(handle);

CREATE TABLE IF NOT EXISTS web_items (
    item_id      TEXT PRIMARY KEY,            -- sha1(url)
    source_id    INTEGER,
    url          TEXT NOT NULL,
    title        TEXT DEFAULT '',
    text_excerpt TEXT DEFAULT '',
    published_at TEXT,
    relevant     INTEGER,
    category     TEXT,
    importance   INTEGER,
    one_line     TEXT,
    ingested_at  TEXT NOT NULL,
    scored_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_web_items_ingested ON web_items(ingested_at);
CREATE INDEX IF NOT EXISTS idx_web_items_source ON web_items(source_id);

CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,             -- 'ig' | 'site' | 'rss'
    key           TEXT NOT NULL,             -- handle or domain/url
    reason        TEXT DEFAULT '',
    signal        TEXT DEFAULT '',           -- follower count / domain note
    discovered_via TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|dismissed
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period      TEXT NOT NULL,               -- 'monday' | 'friday'
    path        TEXT,
    item_count  INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    sent_at     TEXT                          -- set only on successful send
);
CREATE INDEX IF NOT EXISTS idx_reports_sent ON reports(sent_at);

CREATE TABLE IF NOT EXISTS credit_log (
    date           TEXT PRIMARY KEY,          -- YYYY-MM-DD (Europe/Istanbul)
    credits_spent  INTEGER NOT NULL DEFAULT 0,
    calls          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS races (
    race_key        TEXT PRIMARY KEY,          -- slug(name)|date_raw
    name            TEXT NOT NULL,
    url             TEXT DEFAULT '',           -- race website (for results)
    location        TEXT DEFAULT '',
    distances       TEXT DEFAULT '',
    race_type       TEXT DEFAULT '',
    notes           TEXT DEFAULT '',
    date_raw        TEXT DEFAULT '',
    start_date      TEXT,                       -- YYYY-MM-DD (best effort)
    end_date        TEXT,                       -- YYYY-MM-DD (best effort)
    is_tr           INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'upcoming',  -- upcoming | completed
    announced       INTEGER NOT NULL DEFAULT 0, -- emitted the upcoming feed entry
    results_fetched INTEGER NOT NULL DEFAULT 0, -- emitted the results feed entry
    results_summary TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_races_tr_status ON races(is_tr, status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def url_hash(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()


@dataclass
class ReconcileResult:
    added: int = 0
    reactivated: int = 0
    deactivated: int = 0


class Store:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or settings.DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- Sources --------------------------------------------------------

    def reconcile_sources(
        self, ig_accounts: Iterable[IGAccount], web_sources: Iterable[WebSource]
    ) -> ReconcileResult:
        """Sync config files into `sources`: new → tracked, removed → inactive.

        Removed lines are marked inactive (active=0), never deleted, so historical
        items keep their source link.
        """
        res = ReconcileResult()
        desired: set[tuple[str, str]] = set()

        with self.tx() as conn:
            for acc in ig_accounts:
                desired.add(("ig", acc.handle))
                self._upsert_source(conn, "ig", acc.handle, acc.note, res)
            for src in web_sources:
                desired.add((src.kind, src.url))
                self._upsert_source(conn, src.kind, src.url, src.note, res)

            # Deactivate sources no longer in the config files.
            for row in conn.execute(
                "SELECT id, kind, key, active FROM sources WHERE active=1"
            ).fetchall():
                if (row["kind"], row["key"]) not in desired:
                    conn.execute(
                        "UPDATE sources SET active=0 WHERE id=?", (row["id"],)
                    )
                    res.deactivated += 1
        return res

    def _upsert_source(
        self, conn: sqlite3.Connection, kind: str, key: str, note: str, res: ReconcileResult
    ) -> None:
        row = conn.execute(
            "SELECT id, active FROM sources WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sources(kind, key, note, active, created_at) "
                "VALUES(?,?,?,1,?)",
                (kind, key, note, now_iso()),
            )
            res.added += 1
        else:
            if not row["active"]:
                res.reactivated += 1
            conn.execute(
                "UPDATE sources SET active=1, note=? WHERE id=?", (note, row["id"])
            )

    def active_sources(self, kind: Optional[str] = None) -> list[sqlite3.Row]:
        if kind:
            return self.conn.execute(
                "SELECT * FROM sources WHERE active=1 AND kind=? ORDER BY key", (kind,)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM sources WHERE active=1 ORDER BY kind, key"
        ).fetchall()

    def get_source(self, kind: str, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sources WHERE kind=? AND key=?", (kind, key)
        ).fetchone()

    def mark_source_run(self, source_id: int, last_seen: Optional[str] = None) -> None:
        with self.tx() as conn:
            if last_seen is not None:
                conn.execute(
                    "UPDATE sources SET last_run_at=?, last_seen=? WHERE id=?",
                    (now_iso(), last_seen, source_id),
                )
            else:
                conn.execute(
                    "UPDATE sources SET last_run_at=? WHERE id=?",
                    (now_iso(), source_id),
                )

    # --- IG posts -------------------------------------------------------

    def ig_post_exists(self, post_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM ig_posts WHERE post_id=?", (post_id,)
            ).fetchone()
            is not None
        )

    def insert_ig_post(
        self,
        post_id: str,
        handle: str,
        url: str,
        caption: str,
        ts: Optional[int],
        likes: int,
        comments: int,
    ) -> bool:
        """Insert a new IG post. Returns True if inserted, False if already present."""
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ig_posts"
                "(post_id, handle, url, caption, ts, likes, comments, ingested_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (post_id, handle, url, caption, ts, likes, comments, now_iso()),
            )
            return cur.rowcount > 0

    def unscored_ig_posts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM ig_posts WHERE relevant IS NULL ORDER BY ts DESC"
        ).fetchall()

    def update_ig_score(
        self,
        post_id: str,
        relevant: bool,
        category: Optional[str],
        importance: Optional[int],
        one_line: Optional[str],
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE ig_posts SET relevant=?, category=?, importance=?, "
                "one_line=?, scored_at=? WHERE post_id=?",
                (
                    1 if relevant else 0,
                    category,
                    importance,
                    one_line,
                    now_iso(),
                    post_id,
                ),
            )

    # --- Web items ------------------------------------------------------

    def web_item_exists(self, item_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM web_items WHERE item_id=?", (item_id,)
            ).fetchone()
            is not None
        )

    def insert_web_item(
        self,
        url: str,
        source_id: Optional[int],
        title: str,
        text_excerpt: str,
        published_at: Optional[str],
    ) -> Optional[str]:
        """Insert a new web item keyed by url hash. Returns item_id if inserted, else None."""
        item_id = url_hash(url)
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO web_items"
                "(item_id, source_id, url, title, text_excerpt, published_at, ingested_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (item_id, source_id, url, title, text_excerpt, published_at, now_iso()),
            )
            return item_id if cur.rowcount > 0 else None

    def unscored_web_items(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM web_items WHERE relevant IS NULL ORDER BY ingested_at DESC"
        ).fetchall()

    def update_web_score(
        self,
        item_id: str,
        relevant: bool,
        category: Optional[str],
        importance: Optional[int],
        one_line: Optional[str],
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE web_items SET relevant=?, category=?, importance=?, "
                "one_line=?, scored_at=? WHERE item_id=?",
                (
                    1 if relevant else 0,
                    category,
                    importance,
                    one_line,
                    now_iso(),
                    item_id,
                ),
            )

    # --- Suggestions ----------------------------------------------------

    def suggestion_exists(self, kind: str, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM suggestions WHERE kind=? AND key=?", (kind, key)
        ).fetchone()

    def add_suggestion(
        self,
        kind: str,
        key: str,
        reason: str,
        signal: str = "",
        discovered_via: str = "",
    ) -> bool:
        """Add a pending suggestion. Returns False if it already exists in any state
        (we never resurface a dismissed candidate)."""
        if self.suggestion_exists(kind, key):
            return False
        with self.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO suggestions"
                "(kind, key, reason, signal, discovered_via, status, created_at) "
                "VALUES(?,?,?,?,?, 'pending', ?)",
                (kind, key, reason, signal, discovered_via, now_iso()),
            )
        return True

    def pending_suggestions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM suggestions WHERE status='pending' ORDER BY created_at DESC"
        ).fetchall()

    def set_suggestion_status(self, suggestion_id: int, status: str) -> bool:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE suggestions SET status=?, decided_at=? WHERE id=?",
                (status, now_iso(), suggestion_id),
            )
            return cur.rowcount > 0

    def set_suggestion_status_by_key(self, kind: str, key: str, status: str) -> bool:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE suggestions SET status=?, decided_at=? WHERE kind=? AND key=?",
                (status, now_iso(), kind, key),
            )
            return cur.rowcount > 0

    def is_dismissed_or_tracked(self, kind: str, key: str) -> bool:
        """True if this candidate is already a suggestion (any state) — used to
        dedupe discovery so dismissed candidates never resurface."""
        return self.suggestion_exists(kind, key) is not None

    # --- Reports --------------------------------------------------------

    def last_report_sent_at(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT sent_at FROM reports WHERE sent_at IS NOT NULL "
            "ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()
        return row["sent_at"] if row else None

    def report_already_sent_today(self, period: str, day: str) -> bool:
        """True if a report of this period was already sent on the given local day
        (day = 'YYYY-MM-DD'). Guards against duplicate sends on re-run."""
        row = self.conn.execute(
            "SELECT 1 FROM reports WHERE period=? AND sent_at IS NOT NULL "
            "AND substr(sent_at,1,10)=? LIMIT 1",
            (period, day),
        ).fetchone()
        return row is not None

    def create_report(self, period: str, path: str, item_count: int) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO reports(period, path, item_count, created_at) "
                "VALUES(?,?,?,?)",
                (period, path, item_count, now_iso()),
            )
            return int(cur.lastrowid)

    def mark_report_sent(self, report_id: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE reports SET sent_at=? WHERE id=?", (now_iso(), report_id)
            )

    def relevant_items_since(
        self, since_iso: Optional[str]
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Return (ig_posts, web_items) that are relevant and ingested since `since_iso`.
        If since_iso is None, returns everything relevant (first report)."""
        if since_iso:
            ig = self.conn.execute(
                "SELECT * FROM ig_posts WHERE relevant=1 AND ingested_at > ? "
                "ORDER BY importance DESC, ts DESC",
                (since_iso,),
            ).fetchall()
            web = self.conn.execute(
                "SELECT * FROM web_items WHERE relevant=1 AND ingested_at > ? "
                "ORDER BY importance DESC, ingested_at DESC",
                (since_iso,),
            ).fetchall()
        else:
            ig = self.conn.execute(
                "SELECT * FROM ig_posts WHERE relevant=1 "
                "ORDER BY importance DESC, ts DESC"
            ).fetchall()
            web = self.conn.execute(
                "SELECT * FROM web_items WHERE relevant=1 "
                "ORDER BY importance DESC, ingested_at DESC"
            ).fetchall()
        return ig, web

    # --- Credit log -----------------------------------------------------

    def credits_spent_today(self, day: str) -> int:
        row = self.conn.execute(
            "SELECT credits_spent FROM credit_log WHERE date=?", (day,)
        ).fetchone()
        return int(row["credits_spent"]) if row else 0

    def record_credits(self, day: str, credits: int, calls: int = 1) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO credit_log(date, credits_spent, calls) VALUES(?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET "
                "credits_spent=credits_spent+excluded.credits_spent, "
                "calls=calls+excluded.calls",
                (day, credits, calls),
            )

    # --- Races ----------------------------------------------------------

    def upsert_race(
        self,
        race_key: str,
        name: str,
        url: str,
        location: str,
        distances: str,
        race_type: str,
        notes: str,
        date_raw: str,
        start_date: Optional[str],
        end_date: Optional[str],
        is_tr: bool,
        status: str,
    ) -> bool:
        """Insert or update a race. Returns True if it was newly inserted.

        Preserves the announced / results_fetched / results_summary flags on
        update; refreshes the descriptive fields and status from the calendar.
        """
        with self.tx() as conn:
            existing = conn.execute(
                "SELECT race_key FROM races WHERE race_key=?", (race_key,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO races(race_key, name, url, location, distances, "
                    "race_type, notes, date_raw, start_date, end_date, is_tr, "
                    "status, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        race_key, name, url, location, distances, race_type, notes,
                        date_raw, start_date, end_date, 1 if is_tr else 0, status,
                        now_iso(), now_iso(),
                    ),
                )
                return True
            conn.execute(
                "UPDATE races SET name=?, url=?, location=?, distances=?, "
                "race_type=?, notes=?, date_raw=?, start_date=?, end_date=?, "
                "is_tr=?, status=?, updated_at=? WHERE race_key=?",
                (
                    name, url, location, distances, race_type, notes, date_raw,
                    start_date, end_date, 1 if is_tr else 0, status, now_iso(),
                    race_key,
                ),
            )
            return False

    def upcoming_tr_races(
        self, today_iso: str, horizon_iso: str, limit: int = 15
    ) -> list[sqlite3.Row]:
        """Nearest upcoming TR races starting within the horizon (for the report's
        live calendar section). Shown each issue, nearest first."""
        return self.conn.execute(
            "SELECT * FROM races WHERE is_tr=1 AND status='upcoming' "
            "AND start_date IS NOT NULL AND start_date >= ? AND start_date <= ? "
            "ORDER BY start_date ASC LIMIT ?",
            (today_iso, horizon_iso, limit),
        ).fetchall()

    def tr_races_for_results(self, floor_iso: str, today_iso: str) -> list[sqlite3.Row]:
        """Recently-completed TR races with a URL whose results we haven't fetched."""
        return self.conn.execute(
            "SELECT * FROM races WHERE is_tr=1 AND results_fetched=0 AND url != '' "
            "AND end_date IS NOT NULL AND end_date < ? AND end_date >= ? "
            "ORDER BY end_date DESC",
            (today_iso, floor_iso),
        ).fetchall()

    def mark_race_results(self, race_key: str, summary: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE races SET results_fetched=1, results_summary=?, "
                "status='completed', updated_at=? WHERE race_key=?",
                (summary, now_iso(), race_key),
            )

    # --- Maintenance / status ------------------------------------------

    def purge_old_raw_items(self, before_iso: str) -> int:
        """Delete raw items older than `before_iso` (retention policy). Returns count.
        Items already summarized into reports are dropped here too — reports keep
        their own one_line snapshot."""
        with self.tx() as conn:
            c1 = conn.execute(
                "DELETE FROM ig_posts WHERE ingested_at < ?", (before_iso,)
            ).rowcount
            c2 = conn.execute(
                "DELETE FROM web_items WHERE ingested_at < ?", (before_iso,)
            ).rowcount
        return c1 + c2

    def counts(self) -> dict[str, int]:
        def n(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "sources_active": n("SELECT COUNT(*) FROM sources WHERE active=1"),
            "ig_posts": n("SELECT COUNT(*) FROM ig_posts"),
            "ig_relevant": n("SELECT COUNT(*) FROM ig_posts WHERE relevant=1"),
            "web_items": n("SELECT COUNT(*) FROM web_items"),
            "web_relevant": n("SELECT COUNT(*) FROM web_items WHERE relevant=1"),
            "suggestions_pending": n(
                "SELECT COUNT(*) FROM suggestions WHERE status='pending'"
            ),
            "races_tr": n("SELECT COUNT(*) FROM races WHERE is_tr=1"),
            "races_tr_upcoming": n(
                "SELECT COUNT(*) FROM races WHERE is_tr=1 AND status='upcoming'"
            ),
            "reports_sent": n("SELECT COUNT(*) FROM reports WHERE sent_at IS NOT NULL"),
        }
