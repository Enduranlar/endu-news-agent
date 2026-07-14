"""Timezone helpers. All scheduling and report dating use Europe/Istanbul.

Storage timestamps stay in UTC ISO (store.now_iso); the *local day* string drives
the credit-log key and report dating so a run at 06:30 Istanbul is attributed to
the right calendar day regardless of the VPS clock's UTC offset.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")


def istanbul_now() -> datetime:
    return datetime.now(ISTANBUL)


def istanbul_day(dt: datetime | None = None) -> str:
    """Return 'YYYY-MM-DD' in Europe/Istanbul."""
    dt = dt or istanbul_now()
    return dt.astimezone(ISTANBUL).strftime("%Y-%m-%d")


def istanbul_weekday_name(dt: datetime | None = None) -> str:
    dt = dt or istanbul_now()
    return dt.astimezone(ISTANBUL).strftime("%A").lower()


_TR_MONTHS = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}
_TR_WEEKDAYS = {
    0: "Pazartesi", 1: "Salı", 2: "Çarşamba", 3: "Perşembe",
    4: "Cuma", 5: "Cumartesi", 6: "Pazar",
}
# Report period -> Turkish day label.
PERIOD_TR = {"monday": "Pazartesi", "friday": "Cuma"}


def istanbul_long_date_tr(dt: datetime | None = None) -> str:
    """Turkish long date, e.g. '30 Haziran 2026, Pazartesi'."""
    dt = (dt or istanbul_now()).astimezone(ISTANBUL)
    return f"{dt.day} {_TR_MONTHS[dt.month]} {dt.year}, {_TR_WEEKDAYS[dt.weekday()]}"


def istanbul_short_date_tr(dt: datetime | None = None) -> str:
    """Turkish short date, e.g. '30 Haziran 2026'."""
    dt = (dt or istanbul_now()).astimezone(ISTANBUL)
    return f"{dt.day} {_TR_MONTHS[dt.month]} {dt.year}"


def cutoff_unix(date_str: str | None) -> int | None:
    """Convert a 'YYYY-MM-DD' ingest floor into a Unix timestamp.

    The date is interpreted as start-of-day (00:00) in Europe/Istanbul. Returns
    None when no floor is set. Raises ValueError on a malformed date.
    """
    if not date_str or not date_str.strip():
        return None
    d = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=ISTANBUL)
    return int(d.timestamp())
