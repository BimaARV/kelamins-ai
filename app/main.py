"""KELA AI - FastAPI application.

Health/liveness/readiness endpoints (spec section 30) plus minimal read-only
API surface used to verify Phase 1 data collection.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, UploadFile, File
from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import __version__
from app.cache import redis_available
from app.collectors.weather.bmkg import fetch_forecast, parse_weather_payload
from app.collectors.weather.locations import resolve_location, search_locations
from app.config import settings
from app.db import Base
from app.db.models import (
    AIInterpretation,
    AIRequest,
    Alert,
    AlertDelivery,
    AlertMessage,
    Article,
    Document,
    Earthquake,
    Event,
    EventArticle,
    EventStatus,
    NetworkCheck,
    ProcessingStatus,
    RelationType,
    Source,
    WeatherForecast,
    WeatherLocation,
)
from app.db.repo import (
    get_ai_interpretations,
    get_or_create_weather_location,
    update_weather_location,
    upsert_weather_forecasts,
)
from app.db.session import engine, get_session
from app.event_engine.confidence import normalize_domain
from app.kela_ai.gateway import AIUnavailable, build_gateway, gateway_definitions
from app.kela_ai.services import process_event

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kela.api")

app = FastAPI(title="KELA AI", version=__version__, docs_url="/docs", openapi_url="/openapi.json")
api = APIRouter(prefix="/api/v1")


@app.get("/")
async def root() -> dict:
    return {"app": "KELA AI", "version": __version__, "env": settings.app_env}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": "KELA AI", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict:
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        logger.warning("database readiness check failed: %s", exc)
    redis_ok = await redis_available()
    body = {
        "status": "ready" if db_ok else "unavailable",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "unavailable",
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if not db_ok:
        from starlette.responses import JSONResponse

        return JSONResponse(status_code=503, content=body)
    return body


@api.get("/sources", response_model=list[dict])
async def list_sources(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "country": s.country,
            "language": s.language,
            "source_type": s.source_type.value,
            "feed_url": s.feed_url,
            "enabled": s.enabled,
        }
        for s in rows
    ]


@api.get("/articles", response_model=list[dict])
async def list_articles(
    limit: int = 20, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    rows = (
        await session.execute(
            select(Article).order_by(desc(Article.scraped_at)).limit(min(limit, 200))
        )
    ).scalars().all()
    return [
        {
            "id": a.id,
            "source_id": a.source_id,
            "title": a.title,
            "url": a.url,
            "published_at": str(a.published_at) if a.published_at else None,
            "scraped_at": str(a.scraped_at) if a.scraped_at else None,
            "language": a.language,
            "processing_status": a.processing_status.value,
        }
        for a in rows
    ]


@api.get("/earthquakes/latest")
async def latest_earthquake(session: AsyncSession = Depends(get_session)) -> dict | None:
    row = (
        await session.execute(
            select(Earthquake).order_by(desc(Earthquake.occurred_at)).limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    return {
        "id": row.id,
        "external_id": row.external_id,
        "magnitude": float(row.magnitude) if row.magnitude is not None else None,
        "depth_km": row.depth_km,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "place": row.place,
        "occurred_at": str(row.occurred_at) if row.occurred_at else None,
        "source": row.source,
    }


@api.get("/network/checks/latest", response_model=list[dict])
async def latest_network_checks(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    subq = (
        select(NetworkCheck.target_id, func.max(NetworkCheck.id).label("max_id"))
        .group_by(NetworkCheck.target_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(NetworkCheck).join(
                subq, NetworkCheck.id == subq.c.max_id
            ).order_by(NetworkCheck.target_id)
        )
    ).scalars().all()
    return [
        {
            "target_id": c.target_id,
            "status": c.status.value,
            "latency_ms": c.latency_ms,
            "packet_loss": c.packet_loss,
            "error_message": c.error_message,
            "checked_at": str(c.checked_at) if c.checked_at else None,
        }
        for c in rows
    ]


@api.get("/weather/locations", response_model=list[dict])
async def weather_locations(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (
        await session.execute(
            select(WeatherLocation).order_by(WeatherLocation.name)
        )
    ).scalars().all()
    return [
        {
            "id": loc.id,
            "name": loc.name,
            "adm4": loc.adm4,
            "provinsi": loc.provinsi,
            "kotkab": loc.kotkab,
            "kecamatan": loc.kecamatan,
            "desa": loc.desa,
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "enabled": loc.enabled,
        }
        for loc in rows
    ]


@api.get("/weather/forecast", response_model=list[dict])
async def weather_forecast(
    location_id: int | None = None,
    limit: int = 24,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = (
        select(WeatherForecast)
        .order_by(desc(WeatherForecast.forecast_datetime))
        .limit(min(limit, 500))
    )
    if location_id is not None:
        stmt = (
            select(WeatherForecast)
            .where(WeatherForecast.location_id == location_id)
            .order_by(desc(WeatherForecast.forecast_datetime))
            .limit(min(limit, 500))
        )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": f.id,
            "location_id": f.location_id,
            "forecast_datetime": str(f.forecast_datetime) if f.forecast_datetime else None,
            "weather_code": f.weather_code,
            "weather": f.weather_description,
            "temperature_c": f.temperature_c,
            "humidity_pct": f.humidity_pct,
            "precipitation_mm": f.precipitation_mm,
            "wind": (
                f"{f.wind_speed_kmh} km/j dari {f.wind_dir}" if f.wind_speed_kmh else None
            ),
            "visibility": f.visibility_text,
        }
        for f in rows
    ]


@api.get("/weather/now", response_model=list[dict])
async def weather_now(session: AsyncSession = Depends(get_session)) -> list[dict]:
    """Nearest-to-now forecast entry per enabled location - compact summary
    readable by KELA AI without touching raw feeds."""
    from datetime import datetime as dt, timedelta, timezone as tz

    now = dt.now(tz.utc).replace(tzinfo=None)
    window_start, window_end = now - timedelta(hours=6), now + timedelta(hours=6)

    locations = (
        await session.execute(
            select(WeatherLocation).where(WeatherLocation.enabled.is_(True))
        )
    ).scalars().all()

    rows = (
        await session.execute(
            select(WeatherForecast, WeatherLocation)
            .join(WeatherLocation, WeatherForecast.location_id == WeatherLocation.id)
            .where(WeatherForecast.forecast_datetime >= window_start)
            .where(WeatherForecast.forecast_datetime <= window_end)
        )
    ).all()

    nearest: dict[int, tuple[WeatherForecast, WeatherLocation]] = {}
    for forecast, loc in rows:
        current = nearest.get(loc.id)
        if current is None or abs((forecast.forecast_datetime - now).total_seconds()) < abs(
            (current[0].forecast_datetime - now).total_seconds()
        ):
            nearest[loc.id] = (forecast, loc)

    out: list[dict] = []
    for loc in locations:
        pair = nearest.get(loc.id)
        if pair is None:
            out.append({"location": loc.name, "kotkab": loc.kotkab, "kecamatan": loc.kecamatan,
                        "desa": loc.desa, "forecast_datetime": None, "weather": None,
                        "temperature_c": None, "humidity_pct": None,
                        "precipitation_mm": None, "wind": None, "visibility": None})
            continue
        f, loc = pair
        out.append(
            {
                "location": loc.name,
                "kotkab": loc.kotkab,
                "kecamatan": loc.kecamatan,
                "desa": loc.desa,
                "forecast_datetime": str(f.forecast_datetime) if f.forecast_datetime else None,
                "weather": f.weather_description,
                "temperature_c": f.temperature_c,
                "humidity_pct": f.humidity_pct,
                "precipitation_mm": f.precipitation_mm,
                "wind": f"{f.wind_speed_kmh} km/j dari {f.wind_dir}" if f.wind_speed_kmh else None,
                "visibility": f.visibility_text,
            }
        )
    out.sort(key=lambda item: item["location"])
    return out


@api.get("/weather/query")
async def weather_query(
    q: str,
    days: int = 3,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """On-demand weather for any BMKG/Kemendagri location (free text or adm4).

    Resolves the query against the shipped Kemendagri index (any village in
    Indonesia), fetches BMKG, stores the location + forecasts, and returns a
    compact summary. Useful both interactively and for future AI lookups.
    """
    candidates = search_locations(q, limit=6)
    if not candidates:
        raise HTTPException(status_code=404, detail={"error": "location not found", "query": q})

    known = (
        await session.execute(
            select(WeatherLocation)
            .where(
                (WeatherLocation.adm4 == q.strip())
                | (WeatherLocation.name.ilike(f"%{q.strip()}%"))
            )
            .limit(1)
        )
    ).scalars().first()
    if known is not None:
        adm4 = known.adm4
    else:
        resolved = resolve_location(q)
        if resolved is None or not resolved.get("village"):
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "no village-level location resolved",
                    "query": q,
                    "candidates": [c["label"] for c in candidates],
                },
            )
        adm4 = resolved["village"]

    try:
        payload = await fetch_forecast(
            adm4, timeout=settings.http_timeout_seconds, endpoint=settings.bmkg_weather_endpoint
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"BMKG fetch failed: {exc}") from exc

    info, rows = parse_weather_payload(payload)
    if not info.get("adm4") or not rows:
        raise HTTPException(
            status_code=502,
            detail={"error": "no forecast data returned by BMKG", "adm4": adm4},
        )

    location = await get_or_create_weather_location(
        session,
        {
            "name": f"{info.get('kecamatan')} - {info.get('desa')}" or info["adm4"],
            "adm1": info.get("adm1"),
            "adm2": info.get("adm2"),
            "adm3": info.get("adm3"),
            "adm4": info["adm4"],
            "provinsi": info.get("provinsi"),
            "kotkab": info.get("kotkab"),
            "kecamatan": info.get("kecamatan"),
            "desa": info.get("desa"),
            "latitude": info.get("latitude"),
            "longitude": info.get("longitude"),
            "timezone": info.get("timezone"),
        },
    )
    await update_weather_location(session, location, info)
    await upsert_weather_forecasts(session, location.id, rows)

    upcoming = sorted(rows, key=lambda row: row["forecast_datetime"])[: min(days * 8, 24)]
    summary = [
        {
            "forecast_datetime": str(row["forecast_datetime"]),
            "weather": row["weather_description"],
            "temperature_c": row["temperature_c"],
            "humidity_pct": row["humidity_pct"],
            "precipitation_mm": row["precipitation_mm"],
            "wind": (
                f"{row['wind_speed_kmh']} km/j dari {row['wind_dir']}"
                if row["wind_speed_kmh"]
                else None
            ),
            "visibility": row["visibility_text"],
        }
        for row in upcoming
    ]
    return {
        "query": q,
        "adm4": location.adm4,
        "location": {
            "id": location.id,
            "name": location.name,
            "adm4": location.adm4,
            "provinsi": location.provinsi,
            "kotkab": location.kotkab,
            "kecamatan": location.kecamatan,
            "desa": location.desa,
        },
        "candidates": candidates,
        "forecast_summary": summary,
    }


def _event_summary(event: Event) -> dict:
    articles = [
        link.article
        for link in event.article_links
        if link.article is not None and link.relation_type != RelationType.duplicate
    ]
    domains = {
        normalize_domain(article.source.base_url)
        for article in articles
        if article.source is not None and article.source.base_url
    }
    return {
        "article_count": len(articles),
        "independent_source_count": len(domains),
    }


def _event_article_out(link: EventArticle) -> dict:
    article = link.article
    return {
        "article_id": article.id,
        "relation_type": link.relation_type.value,
        "similarity_score": (
            float(link.similarity_score) if link.similarity_score is not None else None
        ),
        "source": article.source.name if article.source else None,
        "title": article.title,
        "url": article.url,
        "published_at": str(article.published_at) if article.published_at else None,
    }


@api.get("/events", response_model=list[dict])
async def list_events(
    status: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = (
        select(Event)
        .options(
            selectinload(Event.article_links).selectinload(EventArticle.article).selectinload(
                Article.source
            )
        )
        .order_by(desc(Event.updated_at))
        .limit(min(limit, 500))
    )
    if status in {item.value for item in EventStatus}:
        stmt = stmt.where(Event.status == EventStatus(status))
    if event_type == "news":
        stmt = stmt.where(Event.event_type == "news")
    elif event_type == "earthquake":
        stmt = stmt.where(Event.event_type == "earthquake")
    events = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": event.id,
            "event_type": event.event_type.value,
            "title": event.title,
            "status": event.status.value,
            "confidence": event.confidence.value if event.confidence else None,
            "occurred_at": str(event.occurred_at) if event.occurred_at else None,
            "location_name": event.location_name,
            "updated_at": str(event.updated_at) if event.updated_at else None,
            **_event_summary(event),
        }
        for event in events
    ]


@api.get("/events/{event_id}")
async def get_event(event_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    event = (
        await session.execute(
            select(Event)
            .options(
                selectinload(Event.article_links).selectinload(EventArticle.article).selectinload(
                    Article.source
                )
            )
            .where(Event.id == event_id)
        )
    ).scalars().first()
    if event is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    interpretations = await get_ai_interpretations(session, event_id)
    return {
        "id": event.id,
        "event_type": event.event_type.value,
        "title": event.title,
        "description": event.description,
        "status": event.status.value,
        "confidence": event.confidence.value if event.confidence else None,
        "occurred_at": str(event.occurred_at) if event.occurred_at else None,
        "location_name": event.location_name,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "created_at": str(event.created_at) if event.created_at else None,
        "updated_at": str(event.updated_at) if event.updated_at else None,
        **_event_summary(event),
        "articles": [_event_article_out(link) for link in event.article_links],
        "interpretations": [
            {
                "interpretation_type": item.interpretation_type,
                "content": item.content,
                "created_at": str(item.created_at) if item.created_at else None,
            }
            for item in interpretations
        ],
    }


@api.get("/events/{event_id}/articles", response_model=list[dict])
async def event_articles(
    event_id: int, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    event = (
        await session.execute(
            select(Event)
            .options(
                selectinload(Event.article_links).selectinload(EventArticle.article).selectinload(
                    Article.source
                )
            )
            .where(Event.id == event_id)
        )
    ).scalars().first()
    if event is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    return [_event_article_out(link) for link in event.article_links]


@api.get("/events/{event_id}/interpretations", response_model=list[dict])
async def event_interpretations(
    event_id: int, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    event = (
        await session.execute(select(Event).where(Event.id == event_id))
    ).scalars().first()
    if event is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    rows = await get_ai_interpretations(session, event_id)
    return [
        {
            "interpretation_type": item.interpretation_type,
            "content": item.content,
            "meta": item.meta,
            "created_at": str(item.created_at) if item.created_at else None,
            "updated_at": str(item.updated_at) if item.updated_at else None,
        }
        for item in rows
    ]


@api.post("/ai/process/{event_id}")
async def ai_process(
    event_id: int,
    tasks: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Run KELA AI tasks for one event on demand (summary/classification/
    verification/explanation). Falls back gracefully when the AI pool is
    unavailable - monitoring data is never touched."""
    event = (
        await session.execute(select(Event).where(Event.id == event_id))
    ).scalars().first()
    if event is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    wanted = None
    if tasks:
        wanted = [task.strip() for task in tasks.split(",") if task.strip()]
        valid = {task for task in ("summary", "classification", "verification", "explanation")}
        unknown = set(wanted) - valid
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown tasks: {sorted(unknown)}")
    gateway = build_gateway(settings)
    try:
        completed = await process_event(
            session, event, gateway, tasks=wanted
        )
    except AIUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "ai_unavailable", "code": exc.error_code, "message": str(exc)},
        ) from exc
    return {"event_id": event_id, "completed": completed}


@api.get("/status/ai")
async def status_ai() -> dict:
    gateway = build_gateway(settings)
    from app.cache import get_heartbeat, redis_available

    ai_state = await get_heartbeat("scheduler:ai_state")
    last_ai = await get_heartbeat("scheduler:last_ai_at")
    return {
        "provider": (gateway_definitions() or {}).get("provider"),
        "model": gateway.model,
        "enabled": gateway.configured,
        "state": ai_state or ("no_keys" if not gateway.configured else "active"),
        "last_ai_at": last_ai,
        "keys": gateway.status.get("keys", {}),
        "rate_limiter": gateway.status.get("rate_limiter", {}),
        "circuit_breaker": gateway.status.get("circuit_breaker", {}),
        "models": gateway.status.get("models", []),
        "redis": "ok" if await redis_available() else "unavailable",
    }


@api.get("/status/alerts")
async def status_alerts() -> dict:
    from app.alert_engine.channels import ChannelRegistry
    from app.cache import get_heartbeat, redis_available

    registry = ChannelRegistry.from_settings(settings)
    return {
        "poll_interval_seconds": settings.alert_poll_interval_seconds,
        "channels": registry.summary(),
        "last_alerts_at": await get_heartbeat("scheduler:last_alerts_at"),
        "redis": "ok" if await redis_available() else "unavailable",
    }


# ---------------------------------------------------------------------------
# Phase 5 — Document Engine endpoints
# ---------------------------------------------------------------------------


def _ensure_storage_dir(path: str) -> Path:
    dir_path = Path(path)
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


@api.post("/documents/upload")
async def upload_document(
    event_id: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    file: UploadFile = File(...),
) -> dict:
    """Upload a document (PDF/DOCX/TXT/MD) and extract its text + metadata.

    The upload is capped at DOCUMENT_MAX_UPLOAD_MB. Extracted content is
    stored in the documents table ready for KELA AI processing.
    """
    from app.documents.parser import detect_document_type, parse_document
    from app.db.repo import store_document

    if file.size is not None and file.size > settings.document_max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {settings.document_max_upload_mb} MB limit",
        )

    original_filename = file.filename or "document.bin"
    doc_type = detect_document_type(original_filename)
    if doc_type == "other":
        raise HTTPException(
            status_code=415,
            detail=f"unsupported document type for {original_filename} "
                   "(supported: pdf, docx, txt, md)",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    storage = _ensure_storage_dir(settings.document_storage_path)
    filename = f"{uuid.uuid4().hex}{Path(original_filename).suffix.lower()}"
    file_path = str(storage / filename)
    with open(file_path, "wb") as fh:
        fh.write(content)

    try:
        result = parse_document(file_path, doc_type)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"parse failed: {exc}") from exc

    document = await store_document(
        session,
        filename=filename,
        original_filename=original_filename,
        file_path=file_path,
        document_type=doc_type,
        file_size=len(content),
        text_content=result.text,
        metadata={**result.metadata, "sections": result.sections[:20]},
        event_id=event_id,
    )
    await session.commit()

    return {
        "id": document.id,
        "filename": document.original_filename,
        "document_type": document.document_type.value,
        "file_size": document.file_size,
        "text_length": len(result.text),
        "metadata": document.meta,
        "processing_status": document.processing_status,
    }


@api.get("/documents", response_model_exclude_none=True)
async def list_documents_endpoint(
    document_type: str | None = Query(default=None),
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.db.repo import list_documents

    rows = await list_documents(
        session, limit=min(limit, 100), offset=offset, document_type=document_type
    )
    return {
        "total": len(rows),
        "documents": [
            {
                "id": row.id,
                "filename": row.original_filename,
                "document_type": row.document_type.value,
                "file_size": row.file_size,
                "text_length": len(row.text_content or ""),
                "processing_status": row.processing_status,
                "created_at": str(row.created_at) if row.created_at else None,
            }
            for row in rows
        ],
    }


@api.get("/documents/{doc_id}", response_model_exclude_none=True)
async def get_document_endpoint(
    doc_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.db.repo import get_document

    row = await get_document(session, doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    return {
        "id": row.id,
        "filename": row.original_filename,
        "stored_as": row.filename,
        "document_type": row.document_type.value,
        "file_size": row.file_size,
        "processing_status": row.processing_status,
        "text": row.text_content,
        "metadata": row.meta,
        "event_id": row.event_id,
        "created_at": str(row.created_at) if row.created_at else None,
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }


@api.delete("/documents/{doc_id}")
async def delete_document_endpoint(
    doc_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.db.repo import delete_document

    row = await delete_document(session, doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    try:
        Path(row.file_path).unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("could not remove document file %s: %s", row.file_path, exc)
    await session.commit()
    return {"deleted": True, "id": doc_id, "filename": row.original_filename}


# ---------------------------------------------------------------------------
# Phase 5 — Report generation endpoints
# ---------------------------------------------------------------------------

@api.post("/reports/generate")
async def generate_report(
    title: str = "KELA AI Monitoring Report",
    executive_summary: str = "",
    output_format: str = Query(default="pdf"),
    event_ids: str | None = Query(default=None),
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Generate a PDF/DOCX monitoring report from events.

    Events are selected from the database (optionally filtered by comma-
    separated event_ids), gathered into report data, and rendered via the
    Document Engine. AI summaries can be supplied via executive_summary.
    """
    from app.documents.renderer import render_report

    if output_format not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="output_format must be pdf or docx")

    stmt = (
        select(Event)
        .options(
            selectinload(Event.article_links).selectinload(EventArticle.article).selectinload(Article.source)
        )
        .order_by(desc(Event.updated_at))
        .limit(min(limit, 100))
    )
    if event_ids:
        wanted = [int(x) for x in event_ids.split(",") if x.strip()]
        if not wanted:
            raise HTTPException(status_code=400, detail="empty event_ids list")
        stmt = (
            select(Event)
            .options(
                selectinload(Event.article_links).selectinload(EventArticle.article).selectinload(Article.source)
            )
            .where(Event.id.in_(wanted))
            .order_by(desc(Event.updated_at))
        )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="no events selected for report")

    articles_by_event = {ev.id: [link.article for link in ev.article_links if link.article] for ev in rows}

    report_events = [
        {
            "id": ev.id,
            "event_type": ev.event_type.value,
            "title": ev.title,
            "confidence": ev.confidence.value if ev.confidence else "",
            "status": ev.status.value,
            "occurred_at": str(ev.occurred_at) if ev.occurred_at else "",
            "location_name": ev.location_name,
            "article_count": sum(
                1 for a in articles_by_event.get(ev.id, []) if a is not None
            ),
            "independent_source_count": len(
                {
                    normalize_domain(a.source.base_url)
                    for a in articles_by_event.get(ev.id, [])
                    if a.source and a.source.base_url
                }
            ),
        }
        for ev in rows
    ]

    storage = _ensure_storage_dir(settings.report_storage_path)
    output_name = f"{uuid.uuid4().hex}.{output_format}"
    output_path = str(storage / output_name)
    generated_path = render_report(
        report_events,
        title=title,
        executive_summary=executive_summary,
        output_format=output_format,
        output_path=output_path,
    )

    return {
        "filename": output_name,
        "path": generated_path,
        "format": output_format,
        "event_count": len(report_events),
        "url": f"/api/v1/reports/{output_name}",
    }


@api.get("/reports")
async def list_reports() -> dict:
    storage = _ensure_storage_dir(settings.report_storage_path)
    files = sorted(storage.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    files += sorted(storage.glob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "total": len(files),
        "reports": [
            {
                "filename": f.name,
                "size": f.stat().st_size,
                "modified_at": f.stat().st_mtime,
                "url": f"/api/v1/reports/{f.name}",
            }
            for f in files
        ],
    }


@api.get("/reports/{filename}")
async def download_report(filename: str):
    from starlette.responses import FileResponse

    base_dir = Path(settings.report_storage_path).resolve()
    target = (base_dir / filename).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid report filename")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"report {filename} not found")
    return FileResponse(str(target), filename=filename)


# ---------------------------------------------------------------------------
# Phase 5 — Vision endpoints
# ---------------------------------------------------------------------------

@api.post("/vision/analyze")
async def vision_analyze(
    session: AsyncSession = Depends(get_session),
    prompt: str = Query(default="Describe this image in detail."),
    file: UploadFile = File(...),
) -> dict:
    """Analyze an uploaded image via OCR + (if available) Ollama vision."""
    from app.db.repo import store_document
    from app.vision.analyzer import analyze_image

    original_filename = file.filename or "image.png"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty image")

    storage = _ensure_storage_dir(settings.image_storage_path)
    ext = Path(original_filename).suffix.lower() or ".png"
    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = str(storage / filename)
    with open(file_path, "wb") as fh:
        fh.write(content)

    try:
        result = await analyze_image(
            file_path,
            prompt,
            ocr_fallback=True,
            tesseract_cmd=settings.tesseract_cmd,
            ocr_languages=settings.ocr_languages,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"analysis failed: {exc}") from exc

    document = await store_document(
        session,
        filename=filename,
        original_filename=original_filename,
        file_path=file_path,
        document_type="image",
        file_size=len(content),
        text_content=result.get("text"),
        metadata={"method": result.get("method")},
    )
    await session.commit()
    return {
        "document_id": document.id,
        "method": result.get("method"),
        "text": result.get("text"),
        "error": result.get("error"),
    }


@api.post("/vision/ocr")
async def vision_ocr(
    session: AsyncSession = Depends(get_session),
    file: UploadFile = File(...),
) -> dict:
    """OCR-only endpoint for an uploaded image."""
    from app.db.repo import store_document
    from app.vision.ocr import ocr_image

    original_filename = file.filename or "image.png"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty image")

    storage = _ensure_storage_dir(settings.image_storage_path)
    ext = Path(original_filename).suffix.lower() or ".png"
    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = str(storage / filename)
    with open(file_path, "wb") as fh:
        fh.write(content)

    text = ocr_image(
        file_path,
        languages=settings.ocr_languages,
        tesseract_cmd=settings.tesseract_cmd,
    )

    document = await store_document(
        session,
        filename=filename,
        original_filename=original_filename,
        file_path=file_path,
        document_type="image",
        file_size=len(content),
        text_content=text,
        metadata={"method": "ocr"},
    )
    await session.commit()
    return {
        "document_id": document.id,
        "method": "ocr",
        "text": text,
    }


@api.get("/status/documents")
async def status_documents() -> dict:
    from app.cache import redis_available

    return {
        "storage": {
            "documents": settings.document_storage_path,
            "reports": settings.report_storage_path,
            "images": settings.image_storage_path,
        },
        "tesseract": settings.tesseract_cmd,
        "ocr_languages": settings.ocr_languages,
        "max_upload_mb": settings.document_max_upload_mb,
        "redis": "ok" if await redis_available() else "unavailable",
    }


@api.get("/alerts", response_model_exclude_none=True)
async def list_alerts_endpoint(
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.db.repo import list_alerts

    rows = await list_alerts(session, limit=min(limit, 100), offset=offset)
    return {
        "total": len(rows),
        "alerts": [
            {
                "id": row.id,
                "event_id": row.event_id,
                "priority": row.priority.value,
                "status": row.status,
                "title": row.title,
                "message_count": row.message_count,
                "last_signature": row.last_signature,
                "last_sent_at": row.last_sent_at,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@api.get("/alerts/{alert_id}", response_model_exclude_none=True)
async def get_alert_endpoint(
    alert_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.db.repo import get_alert_by_id

    row = await get_alert_by_id(session, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    return {
        "id": row.id,
        "event_id": row.event_id,
        "event_title": row.event.title if row.event else None,
        "priority": row.priority.value,
        "status": row.status,
        "title": row.title,
        "message_count": row.message_count,
        "last_signature": row.last_signature,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "messages": [
            {
                "id": msg.id,
                "kind": msg.kind.value,
                "priority": msg.priority.value,
                "title": msg.title,
                "body": msg.body,
                "meta": msg.meta,
                "created_at": msg.created_at,
                "deliveries": [
                    {
                        "channel": d.channel,
                        "status": d.status.value,
                        "external_id": d.external_id,
                        "error": d.error,
                    }
                    for d in msg.deliveries
                ],
            }
            for msg in row.messages
        ],
    }


@api.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    counts = {
        "sources": (await session.execute(select(func.count(Source.id)))).scalar() or 0,
        "articles": (await session.execute(select(func.count(Article.id)))).scalar() or 0,
        "events": (await session.execute(select(func.count(Event.id)))).scalar() or 0,
        "earthquakes": (await session.execute(select(func.count(Earthquake.id)))).scalar() or 0,
        "network_checks": (await session.execute(select(func.count(NetworkCheck.id)))).scalar() or 0,
        "weather_forecasts": (
            await session.execute(select(func.count(WeatherForecast.id)))
        ).scalar() or 0,
        "ai_requests": (await session.execute(select(func.count(AIRequest.id)))).scalar() or 0,
        "ai_interpretations": (
            await session.execute(select(func.count(AIInterpretation.id)))
        ).scalar() or 0,
        "alerts": (await session.execute(select(func.count(Alert.id)))).scalar() or 0,
        "alert_messages": (
            await session.execute(select(func.count(AlertMessage.id)))
        ).scalar() or 0,
        "alert_deliveries": (
            await session.execute(select(func.count(AlertDelivery.id)))
        ).scalar() or 0,
        "documents": (await session.execute(select(func.count(Document.id)))).scalar() or 0,
    }
    return {
        "counts": counts,
        "redis": "ok" if await redis_available() else "unavailable",
        "time": datetime.now(timezone.utc).isoformat(),
    }


app.include_router(api)


@app.on_event("shutdown")
async def shutdown() -> None:
    await engine.dispose()