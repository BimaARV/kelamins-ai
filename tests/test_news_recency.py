"""Tests for news recency guards: offset-aware feed datetimes must not crash
store_raw_articles, and stale (older than NEWS_MAX_AGE_HOURS) items are skipped.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Article, Source
from app.db.repo import store_raw_articles


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _source(session):
    src = Source(name="Test", base_url="https://t.example", feed_url="https://t.example/rss", enabled=True)
    session.add(src)
    await session.flush()
    return src.id


def _items(src_id: int, pubs: list[datetime | None]) -> list[dict]:
    return [
        {
            "source_id": src_id,
            "title": f"judul {i}",
            "url": f"https://t.example/{i}",
            "published_at": pub,
        }
        for i, pub in enumerate(pubs)
    ]


async def _published(session) -> list[datetime]:
    rows = (await session.execute(select(Article.published_at))).scalars().all()
    return list(rows)


async def test_store_accepts_offset_aware_datetimes(session):
    """Feeds that hand API an aware UTC datetime must not raise the
    naive-vs-aware compare error — the value is normalized before recency."""
    src_id = await _source(session)
    now = datetime.now(timezone.utc)
    stored = await store_raw_articles(
        session,
        _items(src_id, [now, now - timedelta(hours=1)]),
    )
    assert stored == 2
    pubs = await _published(session)
    assert len(pubs) == 2
    assert all(p.tzinfo is None for p in pubs)


async def test_store_skips_stale_aware_without_crashing(session):
    src_id = await _source(session)
    now = datetime.now(timezone.utc)
    stored = await store_raw_articles(
        session,
        _items(src_id, [now, now - timedelta(days=400)]),
    )
    # The 2025-era aware item is age-filtered (and no compare crash occurs).
    assert stored == 1
    pubs = await _published(session)
    assert len(pubs) == 1
    assert (now.replace(tzinfo=None) - pubs[0]).total_seconds() < 3600


async def test_store_skips_old_naive_published(session):
    src_id = await _source(session)
    stored = await store_raw_articles(session, _items(src_id, [datetime(2025, 7, 26, 10, 0)]))
    assert stored == 0


def test_news_rss_to_utc_is_naive_utc():
    from app.collectors.news.rss import _to_utc

    parsed = time.strptime("19 Sep 2026 06:00:00", "%d %b %Y %H:%M:%S")
    out = _to_utc(parsed)
    assert out is not None
    assert out.tzinfo is None  # naive-UTC on purpose (DB stores naive)
    assert out == datetime(2026, 9, 19, 6, 0, 0)
    assert _to_utc(None) is None
    assert _to_utc(()) is None  # falsy input
    assert _to_utc("garbage") is None