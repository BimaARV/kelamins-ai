"""Collectors package. Each collector fetches raw data from real sources.

AI is never invoked by collectors; raw facts land in MariaDB first.
"""

from app.collectors.earthquake.bmkg import fetch_latest_earthquake, parse_bmkg_gempa
from app.collectors.network.checks import run_check
from app.collectors.news.rss import fetch_and_parse_feed, parse_feed

__all__ = [
    "fetch_and_parse_feed",
    "fetch_latest_earthquake",
    "parse_bmkg_gempa",
    "parse_feed",
    "run_check",
]