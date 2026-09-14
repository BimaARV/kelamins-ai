"""Content-hash duplicate detection (spec section 6: Duplicate Article).

Exact content republication (same canonical text, different URL/source) is
marked ``deduplicated`` and chained to the original article's event as a
``duplicate`` relation so confidence / source independence never double-counts
it as an independent source.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Article,
    EventArticle,
    ProcessingStatus,
    RelationType,
)

logger = logging.getLogger(__name__)

_PROCESSED = (ProcessingStatus.clustered, ProcessingStatus.deduplicated)


async def find_exact_duplicate(session: AsyncSession, article: Article) -> Article | None:
    if not article.content_hash:
        return None
    result = await session.execute(
        select(Article)
        .where(
            Article.content_hash == article.content_hash,
            Article.id != article.id,
            Article.processing_status.in_(_PROCESSED),
        )
        .order_by(Article.id)
        .limit(1)
    )
    return result.scalars().first()


async def mark_deduplicated(
    session: AsyncSession, article: Article, duplicate_of: Article
) -> None:
    article.processing_status = ProcessingStatus.deduplicated
    link = (
        await session.execute(
            select(EventArticle)
            .where(
                EventArticle.article_id == duplicate_of.id,
                EventArticle.relation_type == RelationType.primary,
            )
            .limit(1)
        )
    ).scalars().first()
    if link is not None:
        session.add(
            EventArticle(
                event_id=link.event_id,
                article_id=article.id,
                relation_type=RelationType.duplicate,
                similarity_score=1.0,
            )
        )
    logger.info("article %s marked duplicate of %s", article.id, duplicate_of.id)