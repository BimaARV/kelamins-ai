"""Cluster raw articles into real-world events (spec section 6).

Each unprocessed article is either attached to an existing active/updated event
(as ``related``) or becomes the ``primary`` article of a new event. The pipeline
is deterministic and AI-free; similarity comes from app.event_engine.similarity.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
)
from app.event_engine.dedup import find_exact_duplicate, mark_deduplicated
from app.event_engine.similarity import article_similarity

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _pending_articles(session: AsyncSession) -> list[Article]:
    result = await session.execute(
        select(Article)
        .where(Article.processing_status == ProcessingStatus.raw)
        .order_by(Article.id)
        .limit(settings.event_max_raw_batch)
    )
    return list(result.scalars().all())


async def _event_profiles(session: AsyncSession) -> list[tuple[Event, Article]]:
    rows = (
        await session.execute(
            select(Event, Article)
            .join(EventArticle, EventArticle.event_id == Event.id)
            .join(Article, Article.id == EventArticle.article_id)
            .where(Event.status.in_([EventStatus.active, EventStatus.updated]))
            .where(EventArticle.relation_type == RelationType.primary)
            .order_by(Event.id)
        )
    ).all()
    return [(event, article) for event, article in rows]


def _article_match(article: Article) -> dict:
    return {
        "title": article.title,
        "description": article.description,
        "published_at": article.published_at,
    }


async def cluster_new_articles(session: AsyncSession) -> dict:
    pending = await _pending_articles(session)
    profiles = await _event_profiles(session)
    result = {"processed": 0, "new_events": 0, "related": 0, "duplicates": 0, "failed": 0}

    for article in pending:
        try:
            duplicate_of = await find_exact_duplicate(session, article)
            if duplicate_of is not None:
                await mark_deduplicated(session, article, duplicate_of)
                result["duplicates"] += 1
                result["processed"] += 1
                continue

            best_event, best_score = None, 0.0
            for event, primary in profiles:
                score = article_similarity(
                    _article_match(article), _article_match(primary)
                )
                if score > best_score:
                    best_score, best_event = score, event

            if best_event is not None and best_score >= settings.event_similarity_threshold:
                session.add(
                    EventArticle(
                        event_id=best_event.id,
                        article_id=article.id,
                        relation_type=RelationType.related,
                        similarity_score=best_score,
                    )
                )
                if best_event.status == EventStatus.active:
                    best_event.status = EventStatus.updated
                article.processing_status = ProcessingStatus.clustered
                result["related"] += 1
            else:
                event = Event(
                    event_type=EventType.news,
                    title=article.title[:1000],
                    description=article.description,
                    occurred_at=article.published_at,
                    status=EventStatus.active,
                )
                session.add(event)
                await session.flush()
                session.add(
                    EventArticle(
                        event_id=event.id,
                        article_id=article.id,
                        relation_type=RelationType.primary,
                        similarity_score=1.0,
                    )
                )
                profiles.append((event, article))
                article.processing_status = ProcessingStatus.clustered
                result["new_events"] += 1
            result["processed"] += 1
        except Exception:  # noqa: BLE001 - one bad article never stalls the batch
            result["failed"] += 1
            logger.exception("event clustering failed for article %s", article.id)

    await session.commit()
    return result


async def link_earthquake_events(session: AsyncSession) -> int:
    """Attach an earthquake event wrapper to quakes without one yet.

    The earthquake facts themselves always come from the authoritative BMKG
    feed (spec section 9); the Event row only wraps them for the alert chain.
    """
    quakes = (
        await session.execute(
            select(Earthquake).where(Earthquake.event_id.is_(None))
        )
    ).scalars().all()
    for quake in quakes:
        magnitude = float(quake.magnitude) if quake.magnitude is not None else None
        place = quake.place or quake.source
        title = f"Gempa M{magnitude} {place}".strip() if magnitude else f"Gempa {place}"
        event = Event(
            event_type=EventType.earthquake,
            title=title[:1000],
            occurred_at=quake.occurred_at,
            latitude=float(quake.latitude) if quake.latitude is not None else None,
            longitude=float(quake.longitude) if quake.longitude is not None else None,
            location_name=place,
            confidence=Confidence.high,
            status=EventStatus.active,
        )
        session.add(event)
        await session.flush()
        quake.event_id = event.id
        logger.info("earthquake event created quake=%s title=%s", quake.id, title)
    await session.commit()
    return len(quakes)