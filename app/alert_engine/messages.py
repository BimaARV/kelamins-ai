"""Alert message body builder. Facts only, always with source attribution."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AlertMessageKind,
    AlertPriority,
    Article,
    Earthquake,
    Event,
    EventArticle,
    EventType,
    RelationType,
)
from app.event_engine.confidence import normalize_domain

_LABELS = {
    EventType.earthquake.value: "GEMPA",
    EventType.news.value: "BERITA",
    EventType.network.value: "JARINGAN",
    EventType.other.value: "EVENT",
}

_KIND_TAG = {
    AlertMessageKind.initial: "KEJADIAN BARU",
    AlertMessageKind.update: "PEMBARUAN",
    AlertMessageKind.severity: "PENINGKATAN PRIORITAS",
    AlertMessageKind.resolved: "SELESAI",
}


async def _sources(session: AsyncSession, event_id: int) -> tuple[list[str], str | None]:
    rows = (
        await session.execute(
            select(Article)
            .options(selectinload(Article.source))
            .join(EventArticle, EventArticle.article_id == Article.id)
            .where(
                EventArticle.event_id == event_id,
                EventArticle.relation_type != RelationType.duplicate,
            )
            .order_by(EventArticle.similarity_score.desc())
        )
    ).scalars().all()
    domains = []
    first_url = None
    for article in rows:
        if first_url is None:
            first_url = article.url
        if article.source and article.source.base_url:
            domain = normalize_domain(article.source.base_url)
            if domain and domain not in domains:
                domains.append(domain)
    return domains, first_url


async def _earthquake_detail(
    session: AsyncSession, event_id: int
) -> str | None:
    quake = (
        await session.execute(
            select(Earthquake)
            .where(Earthquake.event_id == event_id)
            .order_by(Earthquake.occurred_at.desc())
        )
    ).scalars().first()
    if quake is None:
        return None
    mag = f"{float(quake.magnitude):.1f}" if quake.magnitude is not None else "-"
    depth = f"{float(quake.depth_km):.0f} km" if quake.depth_km is not None else "-"
    return f"Magnitudo M{mag} | kedalaman {depth} | {quake.place or '-'}"


async def build_alert_body(
    session: AsyncSession,
    event: Event,
    stats: dict,
    priority: AlertPriority,
    kind: AlertMessageKind,
    reason: str,
) -> str:
    sources, first_url = await _sources(session, event.id)
    lines = [
        f"[{priority.value}] {_KIND_TAG.get(kind, kind.value).upper()} — {_LABELS.get(event.event_type.value, 'EVENT')}",
        f"Event #{event.id} | konfirmasi: {event.confidence.value if event.confidence else '-'}"
        f" | {stats['article_count']} artikel | {stats['independent_source_count']} sumber independen",
    ]
    if event.location_name:
        lines.append(f"Lokasi: {event.location_name}")
    if event.occurred_at:
        lines.append(f"Waktu: {event.occurred_at}")
    if event.event_type == EventType.earthquake:
        detail = await _earthquake_detail(session, event.id)
        if detail:
            lines.append(detail)
    if sources:
        lines.append("Sumber: " + ", ".join(sources))
    if reason:
        lines.append("Alasan prioritas: " + reason)
    if first_url:
        lines.append("Referensi: " + first_url)
    lines.append("Dibuat dari data monitoring; fakta mengikuti sumber yang tertera.")
    return "\n".join(lines)