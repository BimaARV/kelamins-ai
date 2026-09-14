"""Event Intelligence engine tests against in-memory SQLite.

Covers dedup by content hash, article -> event clustering, earthquake event
wrapping and confidence scoring, all fully offline / AI-free.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Article,
    Confidence,
    Earthquake,
    Event,
    EventArticle,
    EventStatus,
    EventType,
    ProcessingStatus,
    RelationType,
    Source,
)
from app.event_engine import run_event_intelligence
from app.event_engine.clustering import cluster_new_articles, link_earthquake_events
from app.event_engine.confidence import event_stats

NOW = datetime(2026, 9, 10, 12, 0, 0)


async def _add_source(session, name: str, url: str) -> int:
    source = Source(name=name, base_url=url, feed_url=url + "/rss")
    session.add(source)
    await session.flush()
    return source.id


async def _add_article(session, source_id: int, title: str, url: str, hash_: str, at: datetime):
    session.add(
        Article(
            source_id=source_id,
            title=title,
            url=url,
            description=title + ".",
            content_hash=hash_,
            published_at=at,
            processing_status=ProcessingStatus.raw,
        )
    )
    await session.flush()


async def test_articles_cluster_into_events_and_dedup():
    engine = None
    try:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            src_a = await _add_source(session, "Antara", "https://www.antaranews.com")
            src_b = await _add_source(session, "CNN", "https://www.cnnindonesia.com")
            await session.commit()

            await _add_article(
                session, src_a, "Gempa Magnitudo 5.2 guncang Maluku Barat Daya",
                "https://x/a", "hash-111", NOW,
            )
            await _add_article(
                session, src_b, "Gempa M5.2 Maluku Barat Daya terasa sampai Ambon",
                "https://y/b", "hash-222", NOW + timedelta(hours=2),
            )
            await _add_article(
                session, src_a, "Gempa Magnitudo 5.2 guncang Maluku Barat Daya",
                "https://z/repost", "hash-111", NOW + timedelta(hours=3),
            )
            await session.commit()

            result = await cluster_new_articles(session)
            assert result["processed"] == 3
            assert result["new_events"] == 1
            assert result["related"] == 1
            assert result["duplicates"] == 1

            events = (await session.execute(
                select(Event)
            )).scalars().all()
            assert len(events) == 1
            event = events[0]
            assert event.event_type == EventType.news
            assert event.status == EventStatus.updated

            links = (await session.execute(
                select(EventArticle)
            )).scalars().all()
            relations = {link.relation_type for link in links}
            assert relations == {RelationType.primary, RelationType.related, RelationType.duplicate}
            assert links[0].similarity_score in (1.0, 1)

            statuses = set(article.processing_status for article in
                           (await session.execute(
                               select(Article))).scalars().all())
            assert statuses == {ProcessingStatus.clustered, ProcessingStatus.deduplicated}

            stats = await event_stats(session, event)
            assert stats["article_count"] == 2
            assert stats["independent_source_count"] == 2
            from app.event_engine.confidence import refresh_event_confidences

            await refresh_event_confidences(session)
            assert event.confidence == Confidence.medium
    finally:
        if engine is not None:
            await engine.dispose()


async def test_unrelated_articles_become_separate_events():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            src = await _add_source(session, "Antara", "https://www.antaranews.com")
            await session.commit()
            await _add_article(
                session, src, "Gempa Mag 5.2 guncang Maluku Barat Daya",
                "https://x/1", "h-1", NOW,
            )
            await _add_article(
                session, src, "Pemilu serentak dijadwalkan Jumat mendatang",
                "https://x/2", "h-2", NOW,
            )
            await session.commit()
            result = await cluster_new_articles(session)
            assert result["new_events"] == 2
            assert result["related"] == 0
            events = (await session.execute(
                select(Event)
            )).scalars().all()
            assert len(events) == 2
            from app.event_engine.confidence import refresh_event_confidences

            await refresh_event_confidences(session)
            assert all(event.confidence == Confidence.low for event in events)
    finally:
        await engine.dispose()


async def test_earthquake_wraps_event_high_confidence():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(
                Earthquake(
                    external_id="20260910-01",
                    magnitude=5.2,
                    depth_km=10.0,
                    latitude=-7.0,
                    longitude=125.0,
                    place="Maluku Barat Daya",
                    occurred_at=NOW,
                    source="bmkg",
                )
            )
            await session.commit()
            created = await link_earthquake_events(session)
            assert created == 1
            events = (await session.execute(
                select(Event)
            )).scalars().all()
            assert len(events) == 1
            assert events[0].event_type == EventType.earthquake
            assert events[0].confidence == Confidence.high
            assert events[0].location_name == "Maluku Barat Daya"
    finally:
        await engine.dispose()


async def test_run_event_intelligence_pipeline():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            src = await _add_source(session, "Antara", "https://www.antaranews.com")
            session.add(
                Earthquake(
                    external_id="20260910-02", magnitude=4.9, place="Bengkulu",
                    occurred_at=NOW, source="bmkg",
                )
            )
            await session.commit()
            await _add_article(
                session, src, "Gempa M5.2 guncang Maluku Barat Daya",
                "https://x/a1", "h3", NOW,
            )
            await session.commit()
            result = await run_event_intelligence(session)
            assert result["processed"] == 1
            assert result["earthquake_events"] == 1
            assert result["changed"] is True
    finally:
        await engine.dispose()