# Endurance Sports Nutrition News Agent

An autonomous, cron-driven agent that monitors a curated set of Instagram accounts
(via the **SociaVault** API) and websites/RSS feeds for new activity relevant to an
endurance-sports-nutrition e-commerce business (Turkey + global). It filters every
new item for relevance with an LLM, classifies it into interest categories,
discovers candidate new sources for operator approval, and emails a concise
**summary report twice a week (Monday & Friday morning)** — archiving every report
with a searchable index.

The full build brief and operating guide is in **`news-agent.md`**. This README is
the deployment runbook.

---

## How it works

```
cron 06:30  →  python -m src.main daily
                 ├── reconcile config files → DB (sources)
                 ├── ingest Instagram   (SociaVault, 1 credit/handle)  → new posts
                 ├── ingest web         (RSS + raw-site crawl)         → new articles
                 ├── relevance scoring  (LLM, cheap model)             → relevant + category
                 └── discovery          (IG relateds + web search)     → pending suggestions

cron Mon/Fri 08:00  →  python -m src.main report --period monday|friday
                         ├── pull relevant items since last sent report
                         ├── group by interest category, summarise (LLM, strong model)
                         ├── render Markdown + inline-CSS HTML
                         ├── archive to reports/<YYYY>/<date>_<period>.md (+ index)
                         └── email to REPORT_TO
```

- **State / dedup:** SQLite at `data/agent.db`. Every item is processed at most
  once (IG shortcode / URL hash). Re-runs never duplicate items, double-spend
  credits, or resend email.
- **Credit guardrail:** before each SociaVault call the agent checks today's spend
  against `SOCIAVAULT_DAILY_CREDIT_BUDGET`. Over budget → discovery is skipped
  first, core ingestion continues, the run never hard-crashes.
- **Proxy scope:** the Turkish residential proxy (`OUTBOUND_PROXY_URL`) is used
  **only** for direct `.tr` web fetches that need a Turkish exit IP. SociaVault
  (a global API that scrapes Instagram server-side, so our request IP doesn't
  affect the scrape), LLM, and email **always go direct**. SOCKS5 (`socks5h://…`,
  recommended) and HTTP(S) proxies are both supported.
- **Timezone:** `Europe/Istanbul` for all scheduling and report dating.

---

## Requirements

- Python **3.11+**
- A Linux VPS (cron-driven; no always-on web server needed)
- A **SociaVault** API key and an **Anthropic** API key
- SMTP credentials for sending the report
- (Recommended) a Turkish residential proxy URL (SOCKS5 preferred)

---

## Install (VPS setup)

```bash
# 1. Clone and enter the project
git clone <your-repo> nutrition-news-agent
cd nutrition-news-agent

# 2. Create a virtualenv and install deps
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env
$EDITOR .env          # fill in API keys, SMTP, proxy, REPORT_TO

# 4. Create your source lists from the templates, then edit them
cp config/igaccounts.md.example  config/igaccounts.md
cp config/websites.md.example    config/websites.md
cp config/interests.yaml.example config/interests.yaml
cp config/memory.yaml.example    config/memory.yaml       # optional (see Memory)
cp config/agents.yaml.example    config/agents.yaml       # optional (see Running several models)
$EDITOR config/igaccounts.md     # one IG handle per line
$EDITOR config/websites.md       # rss|<url> or site|<url> per line
$EDITOR config/interests.yaml    # categories + business context (tunes the LLM)
```

The repo ships `config/*.example` **templates**; your real `igaccounts.md`,
`websites.md`, and `interests.yaml` are gitignored so private sources/strategy
never land in the code repo. (To version your real config, keep it in a separate
private repo.)

`data/`, `logs/`, and `reports/` are created automatically on first run.

---

## First-run smoke test

Confirm the SociaVault integration and exact field paths against a **live** call
(Milestone 1) before trusting ingestion:

```bash
.venv/bin/python -m src.main test-sociavault --handle maurten_official
```

You should see parsed posts (shortcode, timestamp, likes) **and** related accounts.
If related accounts are empty, the agent already retries without `trim`; if they're
still empty, inspect the live JSON and adjust the parser paths in
`src/sociavault.py`.

Then run an ingestion + a dry-run report:

```bash
.venv/bin/python -m src.main daily                       # real ingest + scoring
.venv/bin/python -m src.main report --period friday --dry-run   # build, archive, print — no email
.venv/bin/python -m src.main status                      # counts, credit spend, last runs
```

`--dry-run` on `daily` skips credit-spending SociaVault calls (IG ingest +
discovery); web ingestion and relevance scoring still run. `--dry-run` on `report`
builds and archives the report and prints it, but does **not** email or record a
send.

---

## Commands

| Command | Purpose |
|---|---|
| `daily` | Ingest IG + web, score relevance, run discovery. Cron 06:30. `--agent NAME` to limit. |
| `report --period monday\|friday` | Build + archive + email one summary **per agent**. `--force` to resend, `--agent NAME` to limit. |
| `discover` | Run source discovery on its own. |
| `web` | Run the web admin UI (review suggestions, manual add) — bind to your Tailscale IP. |
| `add-site <url> [note]` | Auto-detect a site's RSS/Atom feed and add it to `config/websites.md`. |
| `suggestions` | List pending source suggestions with reasons. |
| `approve --ig <handle>` | Approve an IG suggestion → append to `config/igaccounts.md`. |
| `approve --site <url> [--as rss\|site]` | Approve a website → append to `config/websites.md`. |
| `dismiss --id <n>` | Dismiss a suggestion; it never resurfaces. |
| `status` | Per-agent counts + **LLM cost**, shared credit spend. `--sources` lists sources. |
| `memory [--topic X] [--query Q] [--forget ID] [--purge-expired]` | Inspect / prune what the agent remembers. |
| `test-agents [--agent NAME]` | Check every agent in `agents.yaml` can actually run (tiny live call each). |
| `test-races [--all] [--limit N]` | Fetch + parse the teamrunbo race calendar (no DB writes). |
| `test-sociavault --handle <h>` | Live profile call (field-path check). |

Add `--verbose` for debug logging on any command.

### Web admin interface (over Tailscale)

A lightweight, responsive web UI (phone + desktop) for reviewing source
suggestions without the CLI. It lists pending Instagram + website suggestions —
each **clickable** to open the profile/site in a new tab — with **Ekle** (approve →
appends to the config file) and **Yoksay** (dismiss) buttons, plus forms to
**manually add** an Instagram handle or a website (websites get RSS auto-detection,
same as `add-site`). Manual/approved entries land in `config/igaccounts.md` /
`config/websites.md` and are tracked on the next `daily` run.

```bash
# Bind to your VPS Tailscale IP in .env, then:
python -m src.main web
# or override per-run:
python -m src.main web --host 100.x.y.z --port 8765
```

Config (`.env`):

```
WEB_LISTEN_HOST=100.x.y.z   # your VPS Tailscale IP (default 127.0.0.1)
WEB_LISTEN_PORT=8765
```

> ⚠️ **No authentication.** Bind it to your **Tailscale IP** so it's only reachable
> over your tailnet. Never set `WEB_LISTEN_HOST=0.0.0.0` on a public VPS. It's a
> stdlib server (no extra dependencies); run it under systemd to keep it up.

**Run it continuously (systemd):** a ready-to-use unit is in
[`deploy/endu-web.service`](deploy/endu-web.service) (edit `User` /
`WorkingDirectory` / `ExecStart` paths for your host):

```bash
sudo cp deploy/endu-web.service /etc/systemd/system/endu-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now endu-web.service   # starts now + on boot
journalctl -u endu-web.service -f              # live logs
```

`Restart=always` brings it back after crashes and retries at boot until the
Tailscale IP is assignable. Restart it after editing `.env`:
`sudo systemctl restart endu-web.service`.

### Turkish race tracking (teamrunbo calendar)

The `daily` run also tracks the Turkish race calendar at
[teamrunbo.com/yaristakvimimiz](https://teamrunbo.com/yaristakvimimiz/). It:

- Parses the calendar table and stores Turkish races (detected via the `🇹🇷`
  flag, a Turkish location/province, or a `.tr`/apphurra race-site link) in the
  `races` table, refreshing each race's status (upcoming → completed) from its date.
- Renders a concise **"Yaklaşan Türkiye yarışları"** section in each report — the
  nearest upcoming races within the next 45 days (capped, nearest first), with
  links to the race sites.
- When a Turkish race has recently finished (within ~21 days), fetches its linked
  race page and asks the LLM to extract finishing results (top finishers); when
  found, they appear once in the report's **race results** category. Results
  fetching is capped per run (default 5) to bound time and token cost.

Inspect the parsed calendar without touching the DB:

```bash
python -m src.main test-races            # Turkish races only
python -m src.main test-races --all       # include non-TR races
```

Notes:
- Results extraction is best-effort: if a race posts results on a separate
  JS-rendered tab the agent may not see them — it retries on later runs within the
  window, then stops. Upcoming races never spam the report (they're one curated
  section, not one item each).
- Tune via constants in `src/races.py` (`REPORT_UPCOMING_DAYS`,
  `REPORT_UPCOMING_LIMIT`, `RESULTS_LOOKBACK_DAYS`, `RESULTS_MAX_PER_RUN`).

### Adding a website (auto feed detection)

Instead of editing `config/websites.md` by hand, point the agent at a URL and let
it decide whether the site has an RSS/Atom feed:

```bash
# As a CLI subcommand:
python -m src.main add-site https://www.letsrun.com
python -m src.main add-site velonews.com "cycling news"

# Or as the standalone script (same logic, no API keys needed):
python scripts/add_website.py https://www.outsideonline.com
```

It checks (in order) whether the URL is itself a feed, the page's
`<link rel="alternate">` feed tags, then common feed paths (`/feed`, `/rss.xml`,
…), validating each candidate with feedparser. It writes:

- `rss | <feed-url>` if a real feed is found, or
- `site | <url>` if none is found (the raw-site crawler will handle it).

Duplicates are skipped. `.tr` hosts are fetched through `OUTBOUND_PROXY_URL`.

### Suggestions approval workflow

Discovery only ever writes **pending** suggestions; it never auto-adds sources.
Each twice-weekly report lists them. The operator stays in control:

```bash
python -m src.main suggestions                    # review
python -m src.main approve --ig maurten_official  # → appends to igaccounts.md
python -m src.main approve --site https://example.com --as rss
python -m src.main dismiss --id 42                # never resurfaced
```

Approved sources start being tracked on the next `daily` run (sources reconcile
from the config files into the DB each run; removed lines are marked inactive, not
deleted).

---

## Running several models side by side (agent fleet)

Compare models on the *same* input: create `config/agents.yaml` (copy the
`.example`) and each agent runs the whole pipeline with its own model.

```yaml
agents:
  - name: haiku
    model: anthropic/claude-haiku-4.5       # OpenRouter model id
    summary_model: anthropic/claude-sonnet-4.6
    primary: true
  - name: gemini-flash
    model: google/gemini-2.5-flash
  - name: gpt5-mini
    model: openai/gpt-5-mini
    enabled: false                           # parked, costs nothing
```

**Shared once per run** — so a fleet costs the same SociaVault credits and web
traffic as a single agent: every SociaVault call, all RSS/site fetches, the race
calendar, and the discovery candidate lookups. **Per agent**: database, memory,
suggestions, reports and cost.

```
data/shared.db                  SociaVault credit log + fetch dedup (global)
data/agents/<name>.db           that agent's items, scores, memory, cost
reports/<name>/<YYYY>/...md     that agent's archive (+ its own index)
```

Agents run **in parallel** (thread pool; separate SQLite files, so no
contention). You get **one email per agent**, subject-tagged `[haiku] …`, each
ending with what that report cost. Suggestions from every agent land in the same
`igaccounts.md` / `websites.md` when approved — the web UI merges them and shows
which agents proposed each.

```bash
python -m src.main test-agents           # verify every agent works (do this first)
python -m src.main daily                 # all enabled agents
python -m src.main daily --agent haiku   # just one
python -m src.main status                # per-agent counts + spend
```

### Checking the fleet

After editing `agents.yaml`, verify each agent before trusting a 06:30 cron run:

```bash
python -m src.main test-agents
```

```
[OK  ] haiku         1.2s  $0.00004  openrouter: anthropic/claude-haiku-4.5  +  anthropic/claude-sonnet-4.6
[FAIL] gemini-flash  0.4s  $0.00000  openrouter: google/gemini-2.5-flashh
        └─ RuntimeError: OpenRouter 404: No endpoints found for google/gemini-2.5-flashh

1/2 agent(s) OK · check cost $0.00004
```

It makes **one small live call per model** (fractions of a cent, reported), and
exercises exactly what the pipeline needs: a JSON-schema **structured** call on
the filter model plus a text call on the summary model when it differs. It
**exits non-zero** if any agent fails, so it can gate a deploy. The checks never
touch the agent databases. Typical failures it catches: a mistyped model id, a
model that can't do structured outputs, an expired key, or no credit.

**No `agents.yaml` → single-agent mode**, using `data/agent.db`, `reports/`, and
the models from `.env` — exactly as before.

### Cost tracking

With `OPENROUTER_API_KEY` set, every call's **real cost** (returned by OpenRouter)
is logged per agent and model, and shows up in two places: a **cost panel in the
web admin UI** (today / 7d / 30d per agent) and a **footer line on each report**
("Bu rapor: 84 çağrı · 412k token · $0.31"). `status` prints the same totals.

> Agents pinned with `provider: anthropic` use the direct Anthropic API, which
> reports tokens but not price — those calls log $0. Also pick models that
> support structured outputs (JSON schema); the pipeline relies on them.

---

## Memory (don't repeat the same story every report)

De-duplication above works **within** one report. Memory works **across** reports:
without it, a recurring story ("X yarışının kayıtları açık") is a brand-new item
every time it's posted, so it lands in report after report.

Enable it by creating `config/memory.yaml` (copy the `.example`). It's a policy
file like `interests.yaml` — you list the kinds of facts worth remembering:

```yaml
topics:
  - id: registration_open
    label: "Yarış kayıtlarının açılması"
    description: >
      Bir yarışın kayıtlarının açıldığı duyurusu. Konu (subject) yarışın tam adıdır.
    suppress_repeats: true      # later posts about the same subject are dropped
    ttl_days: 365               # how long the memory stays in effect
settings:
  max_entries_in_prompt: 8
```

How it works:

1. While scoring, the model records a fact as `(topic, subject)` — e.g.
   `registration_open` / *Kanyon Ulubey Ultra Trail* — with a one-line summary.
2. Later items about the **same subject and topic** are flagged as repeats and
   stored `relevant=0`, so they never reach a report. A genuinely new development
   about that subject (registration opened → race finished) still gets through.
3. Entries expire after `ttl_days` and are purged on each `daily` run.

**Context stays small — it never loads the whole memory.** Each entry is indexed
by its subject's tokens; before scoring a batch the agent looks up only the entries
whose tokens actually appear in those items, capped at `max_entries_in_prompt`.
Typical cost is a few short lines, often zero — and it doesn't grow as memory does.
Memory extraction rides along in the existing scoring call, so there are **no extra
LLM round trips**.

Inspect or correct it:

```bash
python -m src.main memory                     # list what it remembers
python -m src.main memory --query kanyon      # search
python -m src.main memory --forget 12         # drop one entry (it can be learned again)
python -m src.main memory --purge-expired
```

Delete `config/memory.yaml` (or leave `topics` empty) to disable memory entirely —
the pipeline then behaves exactly as before.

---

## De-duplication of repeated stories

The same story often arrives from several sources (different IG accounts / sites)
and survives exact dedup (post_id / URL hash). Each report build runs a **report-wide**
LLM pass that clusters items describing the same event **across all categories** and
keeps the single best one (highest importance → most recent), so new reports don't
repeat lines. Any LLM hiccup falls back to keeping everything.

To clean **already-archived** reports the same way (they're rendered Markdown, so
the build-time pass never saw them):

```bash
python scripts/dedupe_reports.py --dry-run    # preview across all archived reports
python scripts/dedupe_reports.py              # rewrite them in place
python scripts/dedupe_reports.py 2026/2026-07-01_friday.md   # a specific file
```

It only touches category item bullets (the intro, upcoming-races and suggestions
sections are left alone), drops any category section left empty, and updates
`reports/index.json` / `index.md` counts. Reports live under `AGENT_STATE_DIR` (if
set), so review the diff and commit with `scripts/sync_state.py` afterwards.

Reports no longer include a source-suggestions section (suggestions are reviewed in
the web admin UI). To strip that section from **older** archived reports:

```bash
python scripts/strip_report_suggestions.py --dry-run    # preview
python scripts/strip_report_suggestions.py              # rewrite in place
```

---

## Resetting local state

To wipe the database, logs, and archived reports (e.g. to start fresh) without
touching your config or `.env`:

```bash
python scripts/reset.py            # lists targets, asks to confirm
python scripts/reset.py --dry-run  # show what would be deleted, delete nothing
python scripts/reset.py --yes      # skip the prompt (for automation)
python scripts/reset.py --db       # only the database (also --logs / --reports)
```

After a reset the next `daily` run treats every source as a first run, so the
`INGEST_SINCE_DATE` floor (or the first-run limits) applies again.

## Separating state into its own repo (Git)

The code and your **operator state** can live in different repos: the code repo
stays clean/shareable, while `config/` (curated sources + business context),
`reports/` (archived briefs), and `data/agent.db` are version-controlled in a
**separate, private** repo. Set `AGENT_STATE_DIR` to a clone of that repo and the
agent reads/writes state there; logs stay local. Unset, everything stays under the
code repo (default — nothing changes).

**One-time setup** (on the Pi):

```bash
# 1. Create a PRIVATE GitHub repo (e.g. endu-news-state) and clone it:
git clone git@github.com:Enduranlar/endu-news-state.git /home/ps/endu-news-state

# 2. Move your current state into it:
mkdir -p /home/ps/endu-news-state/{config,reports,data}
cp -a config/.        /home/ps/endu-news-state/config/
cp -a reports/.       /home/ps/endu-news-state/reports/ 2>/dev/null || true
cp -a data/agent.db   /home/ps/endu-news-state/data/    2>/dev/null || true

# 3. Point the agent at it (in .env):
echo 'AGENT_STATE_DIR=/home/ps/endu-news-state' >> .env

# 4. First sync — drops .gitignore/.gitattributes, commits, pushes:
.venv/bin/python scripts/sync_state.py
```

`scripts/sync_state.py` checkpoints the SQLite WAL (so the committed `agent.db` is
a complete snapshot), then commits and pushes only when something changed. The
transient `*.db-wal`/`-shm` sidecars are gitignored in the state repo and
`agent.db` is marked binary.

> **Cron/systemd need non-interactive push.** Give the state repo an SSH **deploy
> key** with write access (or a credential helper) so `git push` runs without a
> passphrase prompt. Keep the repo **private** — `interests.yaml` holds your
> strategy and the reports are curated intel. (Moving files out of the code repo
> doesn't purge them from its history; scrub separately if that matters.)

The private repo ends up as:

```
endu-news-state/
├── config/   igaccounts.md, websites.md, interests.yaml
├── reports/  index.md, index.json, 2026/*.md
└── data/     agent.db
```

## Cron install (`Europe/Istanbul`)

Set the crontab to Istanbul time. With a path-based install at
`/opt/nutrition-news-agent`:

```cron
CRON_TZ=Europe/Istanbul

# Daily ingest + discovery at 06:30
30 6 * * *  cd /opt/nutrition-news-agent && .venv/bin/python -m src.main daily >> logs/cron.log 2>&1; .venv/bin/python scripts/sync_state.py >> logs/cron.log 2>&1

# Monday report at 08:00
0 8 * * 1   cd /opt/nutrition-news-agent && .venv/bin/python -m src.main report --period monday >> logs/cron.log 2>&1; .venv/bin/python scripts/sync_state.py >> logs/cron.log 2>&1

# Friday report at 08:00
0 8 * * 5   cd /opt/nutrition-news-agent && .venv/bin/python -m src.main report --period friday >> logs/cron.log 2>&1; .venv/bin/python scripts/sync_state.py >> logs/cron.log 2>&1
```

Install with `crontab -e`. The `; …sync_state.py` suffix commits + pushes the state
repo after each run (a `;` not `&&`, so state is still synced even if the job exits
non-zero — e.g. a failed email send). Drop the sync suffix if you're **not** using
a separate state repo (`AGENT_STATE_DIR` unset) — it's a harmless no-op either way.
If your `cron` doesn't honour `CRON_TZ`, convert the hours to the VPS's local
offset (the agent itself always dates reports in Istanbul time regardless). On an
unhandled error or failed email send, the command exits non-zero so cron emails the
operator the captured `logs/cron.log` output.

---

## Configuration reference

All behaviour is driven by `.env` + the three config files. See `.env.example` for
the full variable list. Key knobs:

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_STATE_DIR` | _(empty)_ | Path to a separate git repo holding `config/`, `reports/`, and `data/agent.db`. Unset = state lives under the code repo. See "Separating state into its own repo". |
| `INGEST_SINCE_DATE` | _(empty)_ | `YYYY-MM-DD` floor (Europe/Istanbul). Posts/articles dated before it are ignored at ingestion and never stored. Set on first deploy to avoid backfilling old history. When set, it's the authoritative first-run gate (overrides the `*_FIRST_RUN_LIMIT` counts for dated sources). |
| `SOCIAVAULT_DAILY_CREDIT_BUDGET` | 400 | Hard daily credit ceiling. Discovery degrades first. |
| `LLM_FILTER_MODEL` | `claude-haiku-4-5` | Cheap model for per-item relevance + source vetting. |
| `LLM_SUMMARY_MODEL` | `claude-sonnet-4-6` | Stronger model for the twice-weekly summary. |
| `IG_FIRST_RUN_LIMIT` | 6 | Most-recent N posts ingested on a handle's first run. |
| `SITE_MAX_NEW_PER_RUN` | 15 | Cap on new articles per raw site per run. |
| `RAW_ITEM_RETENTION_DAYS` | 90 | Raw items older than this are purged each `daily` run. |

---

## Logging & observability

- Structured logs to `logs/agent.log` (rotates at ~10 MB, 5 backups).
- Each `daily` run ends with a one-line `RUN SUMMARY` (new IG posts, new web items,
  scored, relevant, new suggestions, credits spent, purged).
- `status` prints current counts, today's credit spend, and last-run times.

---

## Legal / operational notes

- SociaVault fetches **public, logged-off** data — the compliant lane. The agent
  performs **no logged-in scraping**; do not add any.
- The raw-site crawler is conservative (same-domain, article-like links only),
  rate-limited with a polite delay, and identifies itself via a `User-Agent`.
  Respect each site's robots/ToS; remove a `site|` source if asked.
- We don't store personal data beyond what's needed for the report. Raw items are
  **purged after `RAW_ITEM_RETENTION_DAYS` (default 90)**. Reports keep only the
  one-line summary + link.
- The Turkish proxy is scoped to direct `.tr` web fetches only; SociaVault, LLM,
  and email never tunnel through it.

---

## Repository layout

```
nutrition-news-agent/
├── news-agent.md            # build brief + operating guide (read first)
├── README.md                # this file
├── requirements.txt
├── .env.example
├── config/                  # *.example templates are tracked; live files gitignored
│   ├── igaccounts.md.example    # → copy to igaccounts.md (operator-curated IG handles)
│   ├── websites.md.example      # → copy to websites.md   (sites + RSS)
│   ├── interests.yaml.example   # → copy to interests.yaml (categories + business context)
│   └── memory.yaml.example      # → copy to memory.yaml (what to remember; optional)
├── src/
│   ├── main.py              # CLI: daily, report, discover, approve, ...
│   ├── settings.py          # env loading + validation
│   ├── timeutil.py          # Europe/Istanbul helpers
│   ├── config_loader.py     # parse config files; append approved suggestions
│   ├── store.py             # SQLite schema, dedup, credit log, idempotency
│   ├── llm.py               # Anthropic wrapper (filter + summary models)
│   ├── sociavault.py        # API client (auth, proxy, retries, credit tracking)
│   ├── ingest_instagram.py
│   ├── ingest_web.py        # RSS + raw-site article discovery
│   ├── relevance.py         # LLM relevance scoring + categorization
│   ├── discovery.py         # related-accounts + web-search source suggestions
│   ├── races.py             # Turkish race calendar tracking + results extraction
│   ├── webapp.py            # web admin UI (suggestions review + manual add)
│   ├── feed_detect.py       # RSS/Atom auto-detection for `add-site`
│   ├── report.py            # build + render Mon/Fri summary
│   ├── emailer.py           # SMTP send
│   ├── archive.py           # write report files + maintain index
│   └── logging_setup.py
├── deploy/
│   └── endu-web.service     # systemd unit for the web admin UI
├── scripts/
│   ├── add_website.py       # standalone: detect feed + add a site to websites.md
│   ├── sync_state.py        # commit + push the state repo (AGENT_STATE_DIR)
│   ├── dedupe_reports.py    # retroactively de-duplicate archived reports
│   ├── strip_report_suggestions.py  # remove the suggestions section from old reports
│   └── reset.py             # erase db + logs + reports (asks to confirm)
├── data/agent.db            # SQLite (gitignored)
├── reports/                 # archived reports + index.md / index.json
└── logs/agent.log           # rotating log (gitignored)
```
