"""Alert priority computation (spec section 14). Purely fact-driven.

P1 = critical     : significant earthquake, major network outage
P2 = high         : breaking news (2+ independent sources), smaller earthquake,
                    single target down
P3 = normal       : regular important event
P4 = informational: informational update / network fully healthy

Priority never reads AI output - an LLM being down must not change alerting.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    AlertPriority,
    Earthquake,
    Event,
    EventType,
    NetworkCheck,
    NetworkTarget,
)

logger = logging.getLogger(__name__)


async def network_health(session: AsyncSession) -> dict:
    """Latest check per enabled target. A target counts as 'down' when its
    latest check is anything but 'up' (down/timeout/error)."""
    targets = (await session.execute(select(NetworkTarget))).scalars().all()
    if not targets:
        return {"total": 0, "up": 0, "down": 0}
    target_ids = [t.id for t in targets]
    subq = (
        select(NetworkCheck.target_id, func.max(NetworkCheck.id).label("max_id"))
        .where(NetworkCheck.target_id.in_(target_ids))
        .group_by(NetworkCheck.target_id)
        .subquery()
    )
    latest = (
        await session.execute(
            select(NetworkCheck).join(subq, NetworkCheck.id == subq.c.max_id)
        )
    ).scalars().all()
    status_by_target = {check.target_id: check.status.value for check in latest}
    up = sum(1 for status in status_by_target.values() if status == "up")
    return {
        "total": len(target_ids),
        "up": up,
        "down": len(target_ids) - up,
    }


async def compute_priority(
    session: AsyncSession,
    event: Event,
    stats: dict,
    network_state: dict | None = None,
) -> tuple[AlertPriority, str]:
    if event.event_type == EventType.earthquake:
        return await _earthquake_priority(session, event)

    if event.event_type == EventType.network:
        return _network_priority(network_state or {})

    if event.event_type == EventType.news:
        independent = stats.get("independent_source_count", 0)
        if independent >= 2:
            return (
                AlertPriority.p2,
                f"{independent} sumber independen meliput event ini",
            )
        return AlertPriority.p3, "event berjalan normal, satu sumber utama"

    return AlertPriority.p3, "event umum"


async def _earthquake_priority(session: AsyncSession, event: Event) -> tuple[AlertPriority, str]:
    rows = (
        await session.execute(
            select(Earthquake)
            .where(Earthquake.event_id == event.id)
            .order_by(Earthquake.occurred_at.desc())
        )
    ).scalars().all()
    magnitudes = [float(row.magnitude) for row in rows if row.magnitude is not None]
    mag = max(magnitudes) if magnitudes else None
    if mag is None:
        return AlertPriority.p3, "gempa terdeteksi, magnitude belum tercatat"
    significant = settings.alert_earthquake_significant_magnitude
    high = settings.alert_earthquake_high_magnitude
    if mag >= significant:
        return AlertPriority.p1, f"gempa signifikan M{mag:.1f} (>= M{significant:.1f})"
    if mag >= high:
        return AlertPriority.p2, f"gempa M{mag:.1f} (>= M{high:.1f})"
    return AlertPriority.p3, f"gempa kecil M{mag:.1f}"


def _network_priority(state: dict) -> tuple[AlertPriority, str]:
    total = int(state.get("total") or 0)
    down = int(state.get("down") or 0)
    if total == 0:
        return AlertPriority.p4, "tidak ada target network terkonfigurasi"
    if down >= 1 and down / total >= 0.5:
        return AlertPriority.p1, f"gangguan besar: {down}/{total} target down"
    if down >= 1:
        return AlertPriority.p2, f"{down}/{total} target network down"
    return AlertPriority.p4, f"semua {total} target network sehat"