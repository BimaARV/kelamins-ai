"""News collectors (RSS / API / scraper). Raw data is persisted before AI."""

from app.collectors.news.rss import fetch_and_parse_feed, parse_feed

__all__ = ["fetch_and_parse_feed", "parse_feed"]