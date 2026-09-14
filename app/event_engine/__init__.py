"""Event Intelligence pipeline (Phase 2).

Runs every poll cycle inside the scheduler: drain raw article batches through
dedup -> clustering, wrap new earthquakes into events, then refresh confidence
for active events. Deterministic and AI-free so monitoring never stalls when an
AI provider is down (graceful degradation).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.event_engine.clustering import cluster_new_articles, link_earthquake_events
from app.event_engine.confidence import refresh_event_confidences

logger = logging.getLogger(__name__)


async def run_event_intelligence(session: AsyncSession) -> dict:
    clustering = await cluster_new_articles(session)
    earthquake_events = await link_earthquake_events(session)
    confidence_updated = await refresh_event_confidences(session)
    result = {
        **clustering,
        "earthquake_events": earthquake_events,
        "confidence_updated": confidence_updated,
    }
    changed = (
        clustering["processed"] > 0
        or earthquake_events > 0
        or confidence_updated > 0
    )
    result["changed"] = changed
    if changed:
        logger.info("event intelligence run %s", result)
    return result