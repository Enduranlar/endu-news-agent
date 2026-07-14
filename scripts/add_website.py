#!/usr/bin/env python3
"""Add a website to config/websites.md, auto-detecting its RSS/Atom feed.

Usage:
    python scripts/add_website.py <url> [note...]
    python scripts/add_website.py https://www.letsrun.com
    python scripts/add_website.py velonews.com "cycling news"

Visits the URL, checks for an RSS/Atom feed (a <link> tag, the URL itself being a
feed, or a common /feed path), and appends the correct line:
    rss  | <feed-url>   if a feed is found
    site | <url>        if not (the agent crawls it as a raw site)

Reuses OUTBOUND_PROXY_URL from .env (Turkish .tr hosts are fetched via the proxy).
Standalone — needs no API keys.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running directly: `python scripts/add_website.py ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.feed_detect import add_website
from src.logging_setup import setup_logging


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 1

    url = argv[0]
    note = " ".join(argv[1:]).strip()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    setup_logging()
    proxy = os.environ.get("OUTBOUND_PROXY_URL", "").strip() or None

    result = add_website(url, note=note, proxy=proxy)
    d = result.detection

    if d.kind == "rss":
        print(f"✓ Feed found ({d.detected_via}): {d.feed_url}")
        if d.title:
            print(f"  title: {d.title}")
    else:
        print(f"• No feed found — will track as a raw site: {d.site_url}")
        if d.title:
            print(f"  title: {d.title}")

    if result.added:
        print(f"✓ Added to config/websites.md:\n    {result.line}")
        print("  It will be tracked on the next `daily` run.")
    else:
        print(f"– Already in config/websites.md (nothing changed):\n    {result.line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
