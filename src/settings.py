"""Environment + path configuration for the news agent.

Loads `.env` via python-dotenv, validates required secrets, and exposes a single
`Settings` object the rest of the pipeline reads. Fails fast with a clear message
if a required variable is missing. Never logs secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Repo root = parent of the src/ package directory.
ROOT = Path(__file__).resolve().parent.parent

# Load .env early so AGENT_STATE_DIR is available when we compute the paths below.
load_dotenv(ROOT / ".env")

# Operator state (config, reports, DB) can live in a SEPARATE git repo so it's
# version-controlled independently of the code. Point AGENT_STATE_DIR at that
# repo's clone; otherwise state defaults to the code-repo root (unchanged
# behaviour). Logs always stay local under the code repo — not worth versioning.
_state_dir_env = os.environ.get("AGENT_STATE_DIR", "").strip()
STATE_DIR = Path(_state_dir_env).expanduser().resolve() if _state_dir_env else ROOT

CONFIG_DIR = STATE_DIR / "config"
DATA_DIR = STATE_DIR / "data"
REPORTS_DIR = STATE_DIR / "reports"
LOGS_DIR = ROOT / "logs"

IG_ACCOUNTS_FILE = CONFIG_DIR / "igaccounts.md"
WEBSITES_FILE = CONFIG_DIR / "websites.md"
INTERESTS_FILE = CONFIG_DIR / "interests.yaml"
MEMORY_FILE = CONFIG_DIR / "memory.yaml"
DB_FILE = DATA_DIR / "agent.db"
LOG_FILE = LOGS_DIR / "agent.log"


class ConfigError(RuntimeError):
    """Raised when required configuration / secrets are missing or invalid."""


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(
            f"Required environment variable {name} is missing or empty. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # SociaVault
    sociavault_api_key: str
    sociavault_daily_credit_budget: int

    # Anthropic
    anthropic_api_key: str
    llm_filter_model: str
    llm_summary_model: str

    # Proxy (optional). Used only for direct Turkish-site / web fetches that need a
    # Turkish exit IP. SociaVault (a global API that scrapes Instagram server-side)
    # and LLM/email always go direct.
    outbound_proxy_url: str

    # Email
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    report_from: str
    report_to: list[str] = field(default_factory=list)

    # Behaviour knobs
    ingest_since_date: str = ""  # 'YYYY-MM-DD' floor; items before it are ignored
    ig_first_run_limit: int = 6
    web_first_run_limit: int = 6
    site_max_new_per_run: int = 15
    raw_item_retention_days: int = 90

    @property
    def has_proxy(self) -> bool:
        return bool(self.outbound_proxy_url)


def load_settings(require_email: bool = True, require_llm: bool = True) -> Settings:
    """Load and validate settings from the environment.

    `require_email` / `require_llm` let lightweight commands (e.g. `status`,
    `suggestions`) run without every secret present.
    """
    load_dotenv(ROOT / ".env")

    sociavault_api_key = _require("SOCIAVAULT_API_KEY")

    if require_llm:
        anthropic_api_key = _require("ANTHROPIC_API_KEY")
    else:
        anthropic_api_key = _optional("ANTHROPIC_API_KEY")

    if require_email:
        smtp_host = _require("SMTP_HOST")
        smtp_user = _require("SMTP_USER")
        smtp_pass = _require("SMTP_PASS")
        report_from = _require("REPORT_FROM")
        report_to_raw = _require("REPORT_TO")
    else:
        smtp_host = _optional("SMTP_HOST")
        smtp_user = _optional("SMTP_USER")
        smtp_pass = _optional("SMTP_PASS")
        report_from = _optional("REPORT_FROM")
        report_to_raw = _optional("REPORT_TO")

    report_to = [addr.strip() for addr in report_to_raw.split(",") if addr.strip()]

    ingest_since_date = _optional("INGEST_SINCE_DATE")
    if ingest_since_date:
        try:
            from datetime import datetime as _dt

            _dt.strptime(ingest_since_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ConfigError(
                f"INGEST_SINCE_DATE must be YYYY-MM-DD, got {ingest_since_date!r}"
            ) from exc

    return Settings(
        sociavault_api_key=sociavault_api_key,
        sociavault_daily_credit_budget=_int("SOCIAVAULT_DAILY_CREDIT_BUDGET", 400),
        anthropic_api_key=anthropic_api_key,
        llm_filter_model=_optional("LLM_FILTER_MODEL", "claude-haiku-4-5"),
        llm_summary_model=_optional("LLM_SUMMARY_MODEL", "claude-sonnet-4-6"),
        outbound_proxy_url=_optional("OUTBOUND_PROXY_URL"),
        smtp_host=smtp_host,
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        report_from=report_from,
        report_to=report_to,
        ingest_since_date=ingest_since_date,
        ig_first_run_limit=_int("IG_FIRST_RUN_LIMIT", 6),
        web_first_run_limit=_int("WEB_FIRST_RUN_LIMIT", 6),
        site_max_new_per_run=_int("SITE_MAX_NEW_PER_RUN", 15),
        raw_item_retention_days=_int("RAW_ITEM_RETENTION_DAYS", 90),
    )


def ensure_dirs() -> None:
    """Create runtime directories if they don't exist (idempotent)."""
    for d in (CONFIG_DIR, DATA_DIR, REPORTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
