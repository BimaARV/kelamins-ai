"""Event confidence scoring (spec section 8) + source independence tolerance.

Confidence reflects independent-source count and time consistency, never the
raw article count (spec section 7: 10 articles can mean 3 independent sources).
"""

from __future__ import annotations

import logging
from collections import Counter
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Article,
    Confidence,
    Event,
    EventArticle,
    EventStatus,
    EventType,
    RelationType,
)

logger = logging.getLogger(__name__)

_ACTIVE = (EventStatus.active, EventStatus.updated)


def normalize_domain(base_url: str | None) -> str:
    if not base_url:
        return ""
    try:
        host = urlsplit(base_url).netloc or base_url
    except ValueError:
        host = base_url
    return host.lower().removeprefix("www.")


async def event_stats(session: AsyncSession, event: Event) -> dict:
    rows = (
        await session.execute(
            select(Article)
            .options(selectinload(Article.source))
            .join(EventArticle, EventArticle.article_id == Article.id)
            .where(EventArticle.event_id == event.id)
            .where(EventArticle.relation_type != RelationType.duplicate)
        )
    ).scalars().all()
    domains = Counter(
        normalize_domain(article.source.base_url) for article in rows if article.source
    )
    independent = sum(1 for domain in domains if domain)
    published = [article.published_at for article in rows if article.published_at]
    spread_hours = None
    if published:
        spread_hours = (max(published) - min(published)).total_seconds() / 3600.0
    return {
        "article_count": len(rows),
        "independent_source_count": independent,
        "spread_hours": spread_hours,
    }


def compute_confidence(stats: dict, event_type: EventType) -> tuple[Confidence, str]:
    independent = stats["independent_source_count"]
    spread = stats["spread_hours"]
    consistent_time = spread is None or spread <= 36.0

    if event_type == EventType.earthquake:
        return Confidence.high, "authoritative earthquake feed"

    if independent >= 3 and consistent_time:
        return Confidence.high, f"{independent} independent sources, timeline consistent"
    if independent >= 2:
        return Confidence.medium, (
            f"{independent} independent sources, partial confirmation"
            if consistent_time
            else f"{independent} independent sources, timeline spread"
        )
    return Confidence.low, "single source or details unverified"


async def refresh_event_confidences(session: AsyncSession) -> int:
    events = (
        await session.execute(
            select(Event).where(Event.status.in_(_ACTIVE))
        )
    ).scalars().all()
    updated = 0
    for event in events:
        stats = await event_stats(session, event)
        confidence, _reason = compute_confidence(stats, event.event_type)
        if confidence != event.confidence:
            event.confidence = confidence
            updated += 1
    await session.commit()
    return updated