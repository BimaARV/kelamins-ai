"""KELA AI task services: turn collected facts into AI artifacts.

Responsibilities:
- build a deterministic context payload from stored facts
- run each task through the gateway (never direct HTTP)
- persist an audit row (ai_requests) and the artifact (ai_interpretations)

AI failure never stops monitoring: every call is isolated; when the whole pool
is unavailable the caller (scheduler) simply pauses the AI loop.
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AIInterpretation,
    AIRequestStatus,
    AIRequestType,
    Article,
    Event,
    EventArticle,
    EventStatus,
    RelationType,
)
from app.db.repo import (
    add_ai_request,
    finish_ai_request,
    upsert_ai_interpretation,
)
from app.kela_ai.gateway import AIResult, AIUnavailable, KELAAIGateway
from app.kela_ai.prompts import (
    build_event_context,
    classification_prompt,
    explanation_prompt,
    summary_prompt,
    verification_prompt,
)

logger = logging.getLogger(__name__)

# interpretation_type -> (prompt builder, ai_requests.request_type)
_TASKS: dict[str, tuple[Callable[..., tuple[str, str]], AIRequestType]] = {
    "summary": (summary_prompt, AIRequestType.summary),
    "classification": (classification_prompt, AIRequestType.classification),
    "verification": (verification_prompt, AIRequestType.verification),
    "explanation": (explanation_prompt, AIRequestType.other),
}


def wanted_tasks(event_type: str) -> list[str]:
    if event_type == "earthquake":
        return ["summary", "explanation"]
    if event_type == "news":
        return ["summary", "classification", "verification"]
    return ["summary"]


def _strip_fences(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


async def build_event_payload(session: AsyncSession, event: Event) -> dict:
    links = (
        (await session.execute(
            select(EventArticle)
            .options(selectinload(EventArticle.article).selectinload(Article.source))
            .where(
                EventArticle.event_id == event.id,
                EventArticle.relation_type != RelationType.duplicate,
            )
            .order_by(EventArticle.similarity_score.desc())
        ))
        .scalars()
        .all()
    )
    articles = []
    for link in links:
        article = link.article
        if article is None:
            continue
        articles.append(
            {
                "relation_type": link.relation_type.value,
                "source": article.source.name if article.source else None,
                "published_at": str(article.published_at) if article.published_at else None,
                "title": article.title,
                "description": article.description,
                "url": article.url,
            }
        )
    return {
        "event": {
            "id": event.id,
            "event_type": event.event_type.value,
            "status": event.status.value,
            "occurred_at": str(event.occurred_at) if event.occurred_at else None,
            "location_name": event.location_name,
            "confidence": event.confidence.value if event.confidence else None,
            "title": event.title,
            "description": event.description,
        },
        "articles": articles,
    }


async def process_event(
    session: AsyncSession,
    event: Event,
    gateway: KELAAIGateway,
    *,
    tasks: list[str] | None = None,
) -> list[dict]:
    """Run the wanted AI tasks for one event. Returns completed interpretations."""
    wanted = tasks or wanted_tasks(event.event_type.value)
    payload = await build_event_payload(session, event)
    context = build_event_context(payload)
    completed: list[dict] = []

    for interpretation_type in wanted:
        builder, request_type = _TASKS[interpretation_type]
        system, user = builder(context)
        request = await add_ai_request(
            session,
            provider=gateway.provider,
            key_name=None,
            model=gateway.model,
            request_type=request_type,
            status=AIRequestStatus.running,
        )
        try:
            result: AIResult = await gateway.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
        except AIUnavailable as exc:
            await finish_ai_request(
                session, request, status=AIRequestStatus.failed, error_code=exc.error_code
            )
            raise
        except Exception as exc:  # noqa: BLE001 - never let AI kill the loop
            logger.exception("unexpected AI error for event %s: %s", event.id, exc)
            await finish_ai_request(
                session, request, status=AIRequestStatus.failed, error_code="OTHER"
            )
            raise AIUnavailable(f"unexpected AI error: {exc}", "OTHER") from exc

        if result.provider:
            request.provider = result.provider
        if result.model:
            request.model = result.model
        await finish_ai_request(
            session,
            request,
            status=AIRequestStatus.succeeded,
            key_name=result.key_name,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
        )
        interpretation = await upsert_ai_interpretation(
            session,
            event_id=event.id,
            interpretation_type=interpretation_type,
            content=_strip_fences(result.content),
            meta={
                "model": result.model,
                "provider": result.provider,
                "key_name": result.key_name,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
                "ai_request_id": request.id,
            },
            ai_request_id=request.id,
        )
        completed.append(
            {
                "event_id": event.id,
                "interpretation_type": interpretation_type,
                "content": interpretation.content,
            }
        )
    return completed


async def pick_pending_events(
    session: AsyncSession, limit: int = 5, oversample: int = 3
) -> list[Event]:
    """Events that still lack some wanted AI task, newest first."""
    candidates = (
        (
            await session.execute(
                select(Event)
                .where(
                    Event.status.in_([EventStatus.active.value, EventStatus.updated.value])
                )
                .order_by(Event.updated_at.desc())
                .limit(max(limit, 1) * max(oversample, 1))
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return []
    event_ids = [event.id for event in candidates]
    existing = (
        await session.execute(
            select(AIInterpretation.event_id, AIInterpretation.interpretation_type).where(
                AIInterpretation.event_id.in_(event_ids)
            )
        )
    ).all()
    have: dict[int, set[str]] = {}
    for event_id, interpretation_type in existing:
        have.setdefault(event_id, set()).add(interpretation_type)

    pending = []
    for event in candidates:
        wanted = set(wanted_tasks(event.event_type.value))
        if not wanted.issubset(have.get(event.id, set())):
            pending.append(event)
        if len(pending) >= limit:
            break
    return pending