# CLAUDE.md — Endurance Sports Nutrition News Agent

This file is the build brief **and** the ongoing operating guide for an autonomous
news-monitoring agent. Read it fully before writing code. Build in the milestone
order at the bottom. Prefer simple, debuggable, file-based design over clever
abstractions — this runs unattended on a VPS and must fail loudly and recoverably.

---

## 1. What we are building

An agent that runs **daily** on a VPS and:

1. Follows a curated list of Instagram accounts (via the **SociaVault** API) and a
   curated list of **websites / RSS feeds**, detecting *new* activity since the last run.
2. Filters every new item for relevance against a fixed **interest list** (section 9)
   using an LLM, and classifies relevant items into interest categories.
3. **Discovers new sources**: from Instagram "related accounts" and from web search,
   it proposes new IG accounts and websites to add — each with a one-line reason —
   into a **suggestions queue** for the operator to approve. It never auto-adds.
4. Produces a **summary report twice a week — Monday and Friday morning** — emails it,
   and archives every report to a folder with a searchable index.

The business context is an e-commerce store in endurance sports nutrition (Turkey +
global). Tone of reports: concise, scannable, useful to a busy founder.

---

## 2. Environment & stack

- **Language:** Python 3.11+
- **Runtime:** single VPS, Linux. No always-on web server required (cron-driven).
- **Scheduler:** system `cron` (spec in section 10). Keep all timing in cron, not in code.
- **Storage:** SQLite (`data/agent.db`) for state/dedup + flat Markdown/JSON for reports.
- **HTTP:** `httpx` (with retries/backoff). **Feeds:** `feedparser`.
  **Article text:** `trafilatura`. **Email:** `smtplib` + `email` stdlib.
- **LLM:** Anthropic API. Use a cheap/fast model for per-item relevance filtering and a
  stronger model for writing the twice-weekly summary. Defaults (verify they are current
  at build time): filtering = `claude-haiku-4-5`, summary = `claude-sonnet-4-6`.
- **Proxy:** the VPS has a Turkish residential IP proxy. Route **only** SociaVault and
  any Turkish-site fetches through it via an env var (`OUTBOUND_PROXY_URL`). Make the
  proxy optional per-request so LLM/email traffic can go direct.
- **Timezone:** `Europe/Istanbul` for all scheduling and report dating.

Keep dependencies minimal. Provide `requirements.txt`, `.env.example`, and a `README.md`
with VPS setup steps (venv, cron install, env, first-run smoke test).

---

## 3. Repository layout

```
nutrition-news-agent/
├── CLAUDE.md                  # this file
├── README.md
├── requirements.txt
├── .env.example
├── config/
│   ├── igaccounts.md          # operator-curated IG handles (see §5)
│   ├── websites.md            # operator-curated sites + RSS (see §5)
│   └── interests.yaml         # the interest list / categories (see §9)
├── src/
│   ├── main.py                # CLI entrypoint: `daily`, `report`, `discover`, `approve`
│   ├── config_loader.py       # parse igaccounts.md / websites.md / interests.yaml
│   ├── sociavault.py          # API client (auth, retries, credit tracking)
│   ├── ingest_instagram.py
│   ├── ingest_web.py          # RSS + raw site article discovery
│   ├── discovery.py           # related-accounts + web-search source suggestions
│   ├── relevance.py           # LLM relevance scoring + categorization
│   ├── store.py               # SQLite models + dedup helpers
│   ├── report.py              # build + render Mon/Fri summary
│   ├── emailer.py             # SMTP send
│   └── archive.py             # write report files + maintain index
├── data/
│   └── agent.db               # SQLite (gitignored)
├── reports/                   # archived reports (gitignored or kept, operator choice)
│   ├── index.md
│   ├── index.json
│   └── 2026/
│       └── 2026-06-29_monday.md
└── logs/
    └── agent.log
```

---

## 4. Secrets / environment variables (`.env`)

```
SOCIAVAULT_API_KEY=sk_live_xxx
ANTHROPIC_API_KEY=sk-ant-xxx
OUTBOUND_PROXY_URL=http://user:pass@tr-proxy-host:port   # Turkish residential proxy
SMTP_HOST=...
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
REPORT_FROM=agent@yourstore.com
REPORT_TO=founder@yourstore.com               # comma-separated allowed
SOCIAVAULT_DAILY_CREDIT_BUDGET=400            # hard stop guardrail (see §8)
LLM_FILTER_MODEL=claude-haiku-4-5
LLM_SUMMARY_MODEL=claude-sonnet-4-6
```

Never log secrets. Load via `python-dotenv`. Fail fast with a clear message if any
required var is missing.

---

## 5. Operator-curated source files (input formats)

These are hand-edited by the operator. Parse them tolerantly (ignore blank lines,
allow `#` comments). Keep formats dead simple so a non-developer can edit them.

**`config/igaccounts.md`** — one handle per line, optional inline note after `|`:
```
# Followed Instagram accounts
nike | global brand
maurten_official | gels/nutrition
turkiyeatletizm | TR athletics federation
# add more below
```

**`config/websites.md`** — supports both RSS feeds and raw site URLs. Mark type:
```
# RSS feeds (preferred)
rss | https://www.letsrun.com/feed/
rss | https://www.outsideonline.com/rss/

# Raw sites (no RSS; agent will crawl the listing/homepage for new article links)
site | https://www.iaaf.org/news
site | https://www.athletics.org.tr/haberler
```

The agent must treat both config files as the **source of truth** and reconcile them
into the DB on each run (new lines → tracked; removed lines → marked inactive, not deleted).

---

## 6. SociaVault integration (`sociavault.py`)

- **Base URL:** `https://api.sociavault.com`
- **Auth header:** `X-API-Key: <SOCIAVAULT_API_KEY>` on every request.
- **Route through `OUTBOUND_PROXY_URL`.**
- Authoritative endpoint list & params: `https://docs.sociavault.com/llms.txt`
  and OpenAPI at `https://docs.sociavault.com/api-reference/openapi.json`.
  **Do a live test call during Milestone 1 to confirm exact field paths** before
  hardcoding parsers — response shapes are deeply nested.

Endpoints we use:

| Purpose | Endpoint | Notes |
|---|---|---|
| Profile + recent posts + **related accounts** | `GET /v1/scrape/instagram/profile?handle={h}&trim=true` | **1 credit.** Primary IG call. Use `trim=true` to shrink payload. |
| More posts (pagination) | `GET /v1/scrape/instagram/posts` | Only if profile's embedded recent posts are insufficient. Costs more. |
| Single post/reel detail | `GET /v1/scrape/instagram/post-info` | Optional, for captions/transcripts on flagged posts. |
| Web search (discovery) | `GET /v1/scrape/google/search` | Reuse for source discovery so we don't need a second search vendor. |
| Credit balance | see `api-reference/credits.md` | Poll for the budget guardrail. |

**Parsing the profile response** (confirm exact paths from a live call; documented fields):
- `data.data.user.username`, `.full_name`, `.id`, `.biography`, `.bio_links[].url`
- `.edge_followed_by.count` (followers), `.is_verified`, `.business_category_name`
- Recent posts: `data.data.user.edge_owner_to_timeline_media.edges[].node` with
  `.shortcode` (→ `https://instagram.com/p/{shortcode}`), `.taken_at_timestamp` (Unix),
  `.is_video`, `.edge_liked_by.count`, `.edge_media_to_comment.count`,
  and caption at `.edge_media_to_caption.edges[0].node.text`.
- **Related accounts:** the profile endpoint is documented to return related accounts.
  Inspect the live response for the related-profiles array (Instagram typically exposes
  this as `edge_related_profiles`); if absent under `trim=true`, retry without `trim`.
  Extract handle + full_name for each — these feed discovery (§7).

Implement: typed client methods, exponential backoff on 429/500, and a thin wrapper that
records credits spent per call (read from response headers if present; else estimate).

---

## 7. Pipeline components

### 7a. Instagram ingestion (`ingest_instagram.py`)
For each active handle:
1. Call the profile endpoint (1 credit).
2. For each returned post, compute a stable `post_id` (use shortcode). If not already in
   `ig_posts`, it's **new** → insert and queue for relevance scoring.
3. Track `last_seen_timestamp` per handle so we only ever consider posts newer than what
   we've processed. First run for a handle: ingest only the most recent N (default 6) to
   avoid backfilling years of history.
4. Collect related accounts into the discovery candidate pool (§7c).

### 7b. Web ingestion (`ingest_web.py`)
- **RSS sources:** parse with `feedparser`; each entry keyed by entry id/link hash. New
  links → fetch article, extract main text with `trafilatura`, queue for scoring.
- **Raw sites:** fetch the listing/homepage, extract candidate article links (same-domain,
  look like articles), diff against previously seen URLs for that source, fetch+extract new
  ones. Be conservative: cap new articles per site per run (default 15) and respect a polite
  delay. Route Turkish sites through the proxy.

### 7c. Source discovery (`discovery.py`) — runs as part of the daily job
Two candidate streams, both ending in the **suggestions queue** (never auto-added):
- **IG related accounts** harvested in 7a.
- **Web search:** run a small rotating set of queries built from the interest list and
  brand context (e.g. "ultra marathon Türkiye 2026 takvim", "endurance gel new product",
  "trail running news"). Use SociaVault Google Search. Extract candidate domains + IG handles.

For each unique candidate not already tracked and not already dismissed:
1. Do a lightweight lookup (for IG: one profile call; for sites: fetch homepage title/desc).
2. Ask the LLM: *is this a credible, on-topic source for endurance sports nutrition / racing
   news?* Require a yes/no + a **one-sentence reason** + a credibility note.
3. Insert accepted candidates into `suggestions` with status `pending`, the reason, follower
   count / domain authority signal, and where it was discovered.

Cap discovery lookups per run by the credit budget (§8). Deduplicate aggressively — once a
candidate is dismissed by the operator, never resurface it.

### 7d. Relevance + categorization (`relevance.py`)
Single LLM call per new item (batch where possible to save tokens). Input: source, title,
caption/text excerpt, url, date. Output (strict JSON): `{relevant: bool, category: <one of
interests>, importance: 1-5, one_line: "<=140 char summary>"}`. Drop `relevant:false`.
Keep the prompt anchored to `interests.yaml` so the operator can tune behavior by editing
config, not code.

---

## 8. Storage, dedup & guardrails (`store.py`)

SQLite tables (suggested):
- `sources` — id, kind (`ig`|`rss`|`site`), key (handle/url), note, active, last_run_at, last_seen
- `ig_posts` — post_id, handle, url, caption, ts, likes, comments, relevant, category, importance, one_line, ingested_at
- `web_items` — item_id(url hash), source_id, url, title, text_excerpt, published_at, relevant, category, importance, one_line, ingested_at
- `suggestions` — id, kind, key, reason, signal, discovered_via, status(`pending`|`approved`|`dismissed`), created_at
- `reports` — id, period(`monday`|`friday`), path, item_count, sent_at
- `credit_log` — date, credits_spent, calls

**Dedup rule:** an item is processed at most once, keyed by `post_id` / URL hash. Reports
pull only items with `ingested_at` since the last report of any type.

**Credit guardrail:** before each SociaVault call, check today's spend in `credit_log`
against `SOCIAVAULT_DAILY_CREDIT_BUDGET`. If exceeded, skip remaining discovery (lowest
priority first), log a warning, and continue with core ingestion only. Never hard-crash a
run over budget — degrade gracefully.

**Idempotency:** a re-run on the same day must not duplicate items, double-spend, or resend
email. Guard the report send with a `reports.sent_at` check.

---

## 9. The interest list (`interests.yaml`)

The agent scores and groups items into exactly these categories:

```yaml
categories:
  - id: upcoming_races_tr
    label: "Upcoming races in Turkey"
  - id: upcoming_races_global
    label: "Upcoming big races (global)"
  - id: account_news
    label: "Followed IG accounts — projects / announcements / news"
  - id: records
    label: "Broken records"
  - id: race_results
    label: "Race results — top 3 finishers"
  - id: incidents
    label: "Accidents / safety incidents in sport"
  - id: major_news
    label: "Major sports news"
context: >
  We sell endurance sports nutrition (running, trail, ultra, triathlon, cycling)
  to a Turkish + global audience. Prioritize endurance disciplines and anything
  touching athlete nutrition, fueling, hydration, and product launches.
```

The relevance prompt must read this file so the operator can re-tune scope without code
changes.

---

## 10. Scheduling (cron, `Europe/Istanbul`)

Install these via the README. Daily ingestion runs every morning; reports fire Monday and
Friday after ingestion completes.

```cron
# Daily ingest + discovery at 06:30
30 6 * * *  cd /opt/nutrition-news-agent && /opt/nutrition-news-agent/.venv/bin/python -m src.main daily >> logs/cron.log 2>&1

# Monday report at 08:00
0 8 * * 1   cd /opt/nutrition-news-agent && /opt/nutrition-news-agent/.venv/bin/python -m src.main report --period monday >> logs/cron.log 2>&1

# Friday report at 08:00
0 8 * * 5   cd /opt/nutrition-news-agent && /opt/nutrition-news-agent/.venv/bin/python -m src.main report --period friday >> logs/cron.log 2>&1
```

Set `CRON_TZ=Europe/Istanbul` or convert offsets. The `daily` command also runs discovery.

---

## 11. Reports & archive (`report.py`, `archive.py`)

**`report` command:**
1. Pull relevant items ingested since the last report (Monday report covers the prior
   weekend+week tail; Friday covers Tue–Fri). Use `reports.sent_at` to set the window.
2. Group by interest category (section 9 order). Within a category, sort by `importance`
   desc then recency.
3. Have the summary LLM write a tight brief: a 2–3 sentence top-of-report "what matters
   this week", then per-category bullets (each: one_line + source link + date). Keep it
   skimmable. Include an **"Suggested new sources (pending your approval)"** section listing
   pending `suggestions` with their reasons.
4. Render to Markdown **and** a simple inline-CSS HTML email body.

**Archive:** write each report to `reports/<YYYY>/<YYYY-MM-DD>_<period>.md`, then update:
- `reports/index.json` — append `{date, period, path, item_count, categories_covered}`.
- `reports/index.md` — human-readable reverse-chronological list with links.

**Email (`emailer.py`):** send the HTML report to `REPORT_TO` via SMTP. On send failure,
keep the archived file, log the error, and exit non-zero so cron mail surfaces it. Record
`sent_at` only on success.

---

## 12. Suggestions approval workflow

Discovery only ever writes `pending` suggestions; reports surface them. The operator approves
via a CLI command:

```
python -m src.main approve --ig maurten_official      # moves suggestion → appends to igaccounts.md
python -m src.main approve --site https://example.com # appends to websites.md (asks rss|site)
python -m src.main dismiss --id 42                     # mark dismissed, never resurface
python -m src.main suggestions                         # list pending
```

`approve` updates both the config file and the `suggestions` row. This keeps the operator in
control of list expansion while letting the agent do the legwork. (Optional later: a tiny
web page or a Telegram bot with approve/dismiss buttons — out of scope for v1.)

---

## 13. Logging & observability

- Structured logging to `logs/agent.log` (rotate at ~10MB). One run summary line at the end:
  counts of new IG posts, new web items, relevant items, new suggestions, credits spent.
- On any unhandled exception in a run, log full traceback and exit non-zero (cron emails it).
- A `python -m src.main status` command prints last run times, credit spend today, DB counts.

---

## 14. Legal / operational notes (keep in README)

- SociaVault fetches **public, logged-off** data; that's the compliant lane. Do not add any
  logged-in scraping. Don't store personal data beyond what's needed for the report;
  document purpose and a retention window (e.g., purge raw items older than 90 days).
- Respect each site's robots/ToS for the raw-site crawler; keep request rates polite.
- Keep the Turkish proxy scoped to SociaVault + TR sites; don't tunnel LLM/email through it.

---

## 15. Build milestones (do in order; each must be runnable & testable)

1. **Scaffold + config + SociaVault client.** Repo, env loading, `config_loader`, and a
   verified live SociaVault profile call for ONE handle. Print parsed posts + related
   accounts. Confirm exact JSON field paths here.
2. **Storage + IG ingestion.** SQLite schema, dedup, `daily` ingests all IG handles, stores
   new posts. Idempotent on re-run.
3. **Web ingestion.** RSS + raw-site new-article detection into `web_items`.
4. **Relevance + categorization.** LLM scoring; only relevant items retained, categorized.
5. **Reports + email + archive.** `report --period`, Markdown+HTML, SMTP send, archive index.
6. **Discovery + suggestions + approve CLI.** Related-accounts + web-search candidates,
   LLM vetting, pending queue, approval flow that edits the config files.
7. **Guardrails + cron + docs.** Credit budget, logging, `status`, README with VPS setup,
   cron install, and a dry-run mode (`--dry-run` skips email + credit-spending calls).

**Acceptance test:** seed 3 IG handles + 2 RSS + 1 raw site, run `daily` twice (second run
adds nothing new), run `report --period friday` (email arrives, file archived, index updated),
and confirm at least one pending suggestion appears with a reason.

---

## 16. Conventions for you (Claude Code)

- Small, composable modules; pure functions where practical; type hints throughout.
- Each external call wrapped with retry/backoff and a timeout. No bare `except`.
- Make everything driven by the three config files + `.env`; no hardcoded handles/URLs/keys.
- Provide `--dry-run` everywhere it matters so the operator can test without spending credits
  or emailing.
- Write a short docstring at the top of each module explaining its role in this pipeline.
