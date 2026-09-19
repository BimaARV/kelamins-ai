"""Persistence helpers. Keeps raw data + fact logic out of collectors.

Everything here is raw-first: articles/earthquakes/checks are stored exactly as
collected. No AI is involved in this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import (
    AIInterpretation,
    AIRequest,
    Alert,
    AlertDelivery,
    AlertDeliveryStatus,
    AlertMessage,
    AlertMessageKind,
    Document,
    Earthquake,
    NetworkCheck,
    NetworkTarget,
    Source,
    Article,
    WeatherForecast,
    WeatherLocation,
)

logger = logging.getLogger(__name__)


async def seed_if_empty(session: AsyncSession) -> None:
    """Seed enabled sources + network targets from env fixtures when tables
    are empty. Safe to run on every scheduler start."""
    sources = (await session.execute(select(Source).limit(1))).scalars().first()
    if sources is None:
        fixtures = settings.load_fixtures("news_source_fixtures")
        for item in fixtures:
            session.add(Source(**item))
        await session.commit()
        logger.info("seeded %d source fixtures", len(fixtures))

    targets = (await session.execute(select(NetworkTarget).limit(1))).scalars().first()
    if targets is None:
        fixtures = settings.load_fixtures("network_target_fixtures")
        for item in fixtures:
            session.add(NetworkTarget(**item))
        await session.commit()
        logger.info("seeded %d network target fixtures", len(fixtures))


async def sync_source_fixtures(session: AsyncSession) -> int:
    """Insert source fixtures that are not in the DB yet (matched by base_url).

    Unlike ``seed_if_empty`` this runs even when sources already exist, so adding
    a new RSS feed to NEWS_SOURCE_FIXTURES actually lands in a live database.
    Returns how many new sources were added.
    """
    fixtures = settings.load_fixtures("news_source_fixtures")
    if not fixtures:
        return 0
    existing = set(
        (
            await session.execute(
                select(Source.base_url).where(Source.base_url.is_not(None))
            )
        ).scalars().all()
    )
    added = 0
    for item in fixtures:
        base_url = item.get("base_url")
        if not base_url or base_url in existing:
            continue
        session.add(Source(**item))
        existing.add(base_url)
        added += 1
    if added:
        await session.commit()
        logger.info("synced %d new source fixture(s) into live DB", added)
    return added


async def list_enabled_sources(session: AsyncSession) -> list[Source]:
    result = await session.execute(
        select(Source).where(Source.enabled.is_(True)).order_by(Source.id)
    )
    return list(result.scalars().all())


async def list_enabled_targets(session: AsyncSession) -> list[NetworkTarget]:
    result = await session.execute(
        select(NetworkTarget).where(NetworkTarget.enabled.is_(True)).order_by(NetworkTarget.id)
    )
    return list(result.scalars().all())


async def seed_weather_locations_if_empty(session: AsyncSession) -> None:
    """Seed BMKG weather locations (adm4 codes) from env fixtures."""
    existing = (await session.execute(select(WeatherLocation).limit(1))).scalars().first()
    if existing is not None:
        return
    fixtures = settings.load_fixtures("weather_locations")
    for item in fixtures:
        if not item.get("adm4"):
            continue
        session.add(WeatherLocation(**item))
    await session.commit()
    logger.info("seeded %d weather location fixtures", len(fixtures))


async def sync_weather_locations_if_missing(session: AsyncSession) -> int:
    """Upsert WEATHER_LOCATIONS fixtures so the env watchlist is authoritative.

    Inserts rows that are not in the DB yet AND (re)enables existing rows that
    share the same adm4 — so a location once created ad-hoc (enabled=False via
    /weather/query) lights up again as soon as it's added to WEATHER_LOCATIONS.
    Ad-hoc rows not listed in the fixtures are never touched.
    """
    added = 0
    enabled = 0
    fixtures = settings.load_fixtures("weather_locations")
    for item in fixtures:
        adm4 = item.get("adm4")
        if not adm4:
            continue
        exists = (
            await session.execute(
                select(WeatherLocation).where(WeatherLocation.adm4 == adm4)
            )
        ).scalars().first()
        if exists is None:
            session.add(WeatherLocation(**item))
            added += 1
        elif not getattr(exists, "enabled", True):
            exists.enabled = True
            enabled += 1
    if added or enabled:
        await session.commit()
        logger.info("synced weather fixtures: %d added, %d re-enabled", added, enabled)
    return added


async def get_weather_location_by_adm4(
    session: AsyncSession, adm4: str
) -> WeatherLocation | None:
    return (
        await session.execute(
            select(WeatherLocation).where(WeatherLocation.adm4 == adm4)
        )
    ).scalars().first()


async def get_or_create_weather_location(
    session: AsyncSession, defaults: dict
) -> WeatherLocation:
    """Fetch a weather location by adm4 or create it (used by on-demand query)."""
    adm4 = defaults.get("adm4")
    existing = await get_weather_location_by_adm4(session, adm4)
    if existing is not None:
        existing.enabled = True
        for key, value in defaults.items():
            if value is not None and getattr(existing, key, None) is None:
                setattr(existing, key, value)
        await session.commit()
        return existing
    location = WeatherLocation(**defaults)
    session.add(location)
    await session.commit()
    await session.refresh(location)
    return location


async def list_enabled_weather_locations(session: AsyncSession) -> list[WeatherLocation]:
    result = await session.execute(
        select(WeatherLocation).where(WeatherLocation.enabled.is_(True)).order_by(WeatherLocation.id)
    )
    return list(result.scalars().all())


async def update_weather_location(
    session: AsyncSession, location: WeatherLocation, info: dict
) -> None:
    """Refresh cached admin labels / coordinates returned by BMKG."""
    changed = False
    for key, value in info.items():
        if value is not None and getattr(location, key, None) != value:
            setattr(location, key, value)
            changed = True
    if changed:
        await session.commit()


async def upsert_weather_forecasts(
    session: AsyncSession, location_id: int, rows: list[dict]
) -> int:
    """Insert or update forecast rows; returns number of rows upserted."""
    upserted = 0
    for row in rows:
        exists = (
            await session.execute(
                select(WeatherForecast.id).where(
                    WeatherForecast.location_id == location_id,
                    WeatherForecast.forecast_datetime == row["forecast_datetime"],
                )
            )
        ).first()
        payload = {**row, "location_id": location_id}
        if exists:
            (
                await session.execute(
                    WeatherForecast.__table__.update()
                    .where(WeatherForecast.id == exists[0])
                    .values(**{k: v for k, v in payload.items()})
                )
            )
        else:
            session.add(WeatherForecast(**payload))
        upserted += 1
    await session.commit()
    return upserted


async def store_raw_articles(session: AsyncSession, items: list[dict]) -> int:
    """Insert raw articles, skipping rows whose URL already exists (dedup by
    unique URL in Phase 1; content-hash dedup arrives in the Event Engine).

    Articles older than ``NEWS_MAX_AGE_HOURS`` (by their published_at) are
    skipped entirely so stale feed backlog (e.g. a 2025 item served by a feed
    in 2026) never re-enters the database.
    """
    max_age_hours = int(settings.news_max_age_hours or 0)
    cutoff = None
    if max_age_hours > 0:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=max_age_hours)
    stored = skipped = 0
    for item in items:
        existing = (await session.execute(select(Article.id).where(Article.url == item["url"]))).first()
        if existing:
            continue
        pub = item.get("published_at")
        # Feed parsers may hand us timezone-aware datetimes while the DB and
        # cutoff are naive-UTC — normalize before comparing so feeds never
        # crash with "can't compare offset-naive and offset-aware datetimes".
        if pub is not None and getattr(pub, "tzinfo", None) is not None:
            pub = pub.replace(tzinfo=None)
        if cutoff is not None and pub is not None and pub < cutoff:
            skipped += 1
            continue
        session.add(Article(**item))
        stored += 1
    if skipped:
        logger.info("store_raw_articles: skipped %d stale item(s) older than %dh", skipped, max_age_hours)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        logger.warning("integrity error while storing articles: %s", exc)
    return stored


async def purge_stale_articles(session: AsyncSession, max_age_hours: int | None = None) -> int:
    """Delete articles older than NEWS_MAX_AGE_HOURS (by published_at).

    Keeps the ``articles`` table clean of stale backlog that was ingested
    before the recency filter existed. Runs once at scheduler startup
    (idempotent) and on demand. Returns the number of rows removed.
    """
    max_age_hours = max_age_hours if max_age_hours is not None else int(settings.news_max_age_hours or 0)
    if max_age_hours <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=max_age_hours)
    result = await session.execute(
        delete(Article).where(Article.published_at.is_not(None), Article.published_at < cutoff)
    )
    await session.commit()
    removed = result.rowcount or 0
    if removed:
        logger.info("purge_stale_articles: removed %d article(s) older than %dh", removed, max_age_hours)
    return removed


async def upsert_earthquake(session: AsyncSession, fact: dict | None) -> bool:
    """Upsert an earthquake by (source, external_id). Returns True when new."""
    if not fact:
        return False
    source = fact["source"]
    external_id = fact["external_id"]
    existing = (
        await session.execute(
            select(Earthquake).where(
                Earthquake.source == source, Earthquake.external_id == external_id
            )
        )
    ).scalars().first()
    if existing is not None:
        for key, value in fact.items():
            if key in {"created_at", "updated_at"}:
                continue
            setattr(existing, key, value)
        await session.commit()
        return False
    session.add(Earthquake(**fact))
    await session.commit()
    return True


async def store_network_check(
    session: AsyncSession, target_id: int, result: dict
) -> None:
    session.add(
        NetworkCheck(
            target_id=target_id,
            status=result["status"],
            latency_ms=result.get("latency_ms"),
            packet_loss=result.get("packet_loss"),
            error_message=result.get("error_message"),
            checked_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()


async def add_ai_request(
    session: AsyncSession,
    *,
    provider: str,
    key_name: str | None,
    model: str,
    request_type,
    status,
    error_code: str | None = None,
) -> AIRequest:
    """Persist an AI request row. Keys are never stored - only key_name."""
    row = AIRequest(
        provider=provider,
        key_name=key_name,
        model=model,
        request_type=request_type,
        status=status,
        error_code=error_code,
    )
    session.add(row)
    await session.flush()
    return row


async def finish_ai_request(
    session: AsyncSession,
    request: AIRequest,
    *,
    status,
    key_name: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    error_code: str | None = None,
) -> None:
    request.status = status
    if key_name is not None:
        request.key_name = key_name
    request.input_tokens = input_tokens
    request.output_tokens = output_tokens
    request.latency_ms = latency_ms
    request.error_code = error_code
    await session.commit()


async def get_ai_interpretations(
    session: AsyncSession, event_id: int
) -> list[AIInterpretation]:
    result = await session.execute(
        select(AIInterpretation)
        .where(AIInterpretation.event_id == event_id)
        .order_by(AIInterpretation.interpretation_type)
    )
    return list(result.scalars().all())


async def upsert_ai_interpretation(
    session: AsyncSession,
    *,
    event_id: int,
    interpretation_type: str,
    content: str,
    meta: dict | None = None,
    ai_request_id: int | None = None,
) -> AIInterpretation:
    existing = (
        await session.execute(
            select(AIInterpretation).where(
                AIInterpretation.event_id == event_id,
                AIInterpretation.interpretation_type == interpretation_type,
            )
        )
    ).scalars().first()
    if existing is not None:
        existing.content = content
        existing.meta = meta
        if ai_request_id is not None:
            existing.ai_request_id = ai_request_id
        await session.commit()
        return existing
    row = AIInterpretation(
        event_id=event_id,
        interpretation_type=interpretation_type,
        content=content,
        meta=meta,
        ai_request_id=ai_request_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def count_ai_requests(session: AsyncSession) -> int:
    value = await session.execute(select(func.count(AIRequest.id)))
    return int(value.scalar() or 0)


async def get_alert_for_event(session: AsyncSession, event_id: int) -> Alert | None:
    result = await session.execute(
        select(Alert).options(selectinload(Alert.messages)).where(Alert.event_id == event_id)
    )
    return result.scalars().first()


async def add_alert_message(
    session: AsyncSession,
    *,
    alert_id: int,
    kind: AlertMessageKind,
    priority,
    title: str,
    body: str,
    meta: dict | None = None,
) -> AlertMessage:
    row = AlertMessage(
        alert_id=alert_id,
        kind=kind,
        priority=priority,
        title=title,
        body=body,
        meta=meta,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def add_alert_delivery(
    session: AsyncSession,
    *,
    alert_message_id: int,
    channel: str,
    status: AlertDeliveryStatus,
    external_id: str | None = None,
    error: str | None = None,
) -> AlertDelivery:
    row = AlertDelivery(
        alert_message_id=alert_message_id,
        channel=channel,
        status=status,
        external_id=external_id,
        error=error,
    )
    session.add(row)
    await session.flush()
    return row


async def count_alerts(session: AsyncSession) -> int:
    value = await session.execute(select(func.count(Alert.id)))
    return int(value.scalar() or 0)


async def list_alerts(
    session: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    kind: str | None = None,
) -> list[Alert]:
    query = select(Alert).order_by(Alert.updated_at.desc())
    if kind in ("initial", "update", "severity", "resolved"):
        matches_kind = (
            select(AlertMessage.id)
            .where(AlertMessage.alert_id == Alert.id)
            .where(AlertMessage.kind == AlertMessageKind(kind))
        )
        query = query.where(matches_kind.exists())
    result = await session.execute(query.offset(offset).limit(limit))
    return list(result.scalars().all())


async def get_alert_by_id(session: AsyncSession, alert_id: int) -> Alert | None:
    result = await session.execute(
        select(Alert)
        .options(
            selectinload(Alert.messages).selectinload(AlertMessage.deliveries),
            selectinload(Alert.event),
        )
        .where(Alert.id == alert_id)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Document Engine (Phase 5)
# ---------------------------------------------------------------------------


async def store_document(
    session: AsyncSession,
    *,
    filename: str,
    original_filename: str,
    file_path: str,
    document_type: str,
    file_size: int,
    text_content: str | None = None,
    metadata: dict | None = None,
    event_id: int | None = None,
) -> Document:
    from app.db.models import DocumentType

    doc_type = DocumentType(document_type) if document_type in {e.value for e in DocumentType} else DocumentType.other
    row = Document(
        filename=filename,
        original_filename=original_filename,
        file_path=file_path,
        document_type=doc_type,
        file_size=file_size,
        text_content=text_content,
        meta=metadata,
        processing_status="processed" if text_content else "pending",
        event_id=event_id,
    )
    session.add(row)
    await session.flush()
    return row


async def get_document(session: AsyncSession, doc_id: int) -> Document | None:
    result = await session.execute(select(Document).where(Document.id == doc_id))
    return result.scalars().first()


async def list_documents(
    session: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    document_type: str | None = None,
) -> list[Document]:
    query = select(Document).order_by(Document.created_at.desc())
    if document_type:
        query = query.where(Document.document_type == document_type)
    result = await session.execute(query.offset(offset).limit(limit))
    return list(result.scalars().all())


async def delete_document(session: AsyncSession, doc_id: int) -> Document | None:
    row = await get_document(session, doc_id)
    if row is None:
        return None
    await session.delete(row)
    return row