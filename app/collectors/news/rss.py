"""RSS news collector.

Pipeline step (spec section 5):
    HTTP fetch -> feed parser -> URL validation -> normalization -> raw storage.

This module only turns feed content into normalized dicts; it NEVER calls AI
and never decides real-world events.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from app.normalizer import article_content_hash, canonical_url, is_valid_url

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; KELA-AI/0.1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _to_utc(parsed_time: tuple | None) -> datetime | None:
    if not parsed_time:
        return None
    try:
        # Naive-UTC on purpose: the DB stores naive datetimes and the recency
        # cutoff in repo.py is naive too — mixed aware/naive values crash the
        # `published_at < cutoff` compare at store time.
        return datetime.fromtimestamp(
            calendar.timegm(parsed_time), tz=timezone.utc
        ).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        return None


def _entry_to_dict(entry: Any, source_id: int) -> dict:
    title = str(entry.get("title") or "").strip()
    link = canonical_url(str(entry.get("link") or ""))
    description = str(entry.get("summary") or entry.get("description") or "").strip()

    return {
        "source_id": source_id,
        "title": title,
        "url": link,
        "author": (str(entry.get("author")) if entry.get("author") else None),
        "description": description,
        "content": None,
        "published_at": _to_utc(entry.get("published_parsed") or entry.get("updated_parsed")),
        "scraped_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "content_hash": article_content_hash(title, description, content=None),
        "language": None,
    }


def parse_feed(content: bytes, source_id: int) -> list[dict]:
    """Parse raw feed bytes into a list of normalized article dicts.

    Articles whose URL fails validation are dropped (never invented).
    Produces a deterministic per-source marker for the URL if missing.
    """
    parsed = feedparser.parse(content)
    out: list[dict] = []
    for entry in parsed.entries:
        item = _entry_to_dict(entry, source_id)
        if not is_valid_url(item["url"]):
            logger.warning("dropping entry with invalid URL: %r", item["url"])
            continue
        out.append(item)
    return out


async def fetch_and_parse_feed(
    feed_url: str, source_id: int, timeout: float = 20.0
) -> list[dict]:
    """Fetch a feed over HTTP then parse it. Fails loudly but never halfway."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = await client.get(feed_url)
        resp.raise_for_status()
        return parse_feed(resp.content, source_id)