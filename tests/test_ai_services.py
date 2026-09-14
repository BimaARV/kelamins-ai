"""KELA AI services tests: persisted ai_requests + ai_interpretations, picking."""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AIInterpretation,
    AIRequest,
    AIRequestStatus,
    Article,
    Event,
    EventArticle,
    EventType,
    ProcessingStatus,
    RelationType,
    Source,
)
from app.kela_ai.gateway import AIResult, AIUnavailable
from app.kela_ai.services import (
    pick_pending_events,
    process_event,
    wanted_tasks,
)

FAKE_JSON = '{"summary":"ringkasan kalimat yang sumbernya Antara","key_points":["poin"],"sumber_utama":["Antara"]}'


class FakeGateway:
    model = "fake-model"
    provider = "ollama"

    def __init__(self, fail_after: int | None = None):
        self.calls = 0
        self.fail_after = fail_after

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise AIUnavailable("pool exhausted", "EXHAUSTED")
        return AIResult(
            content=f"```json\n{FAKE_JSON}\n```",
            input_tokens=20,
            output_tokens=10,
            latency_ms=45,
            key_name="K1",
            model="fake-model",
        )


class AlwaysFailsGateway:
    model = "fake-model"
    provider = "ollama"

    async def complete(self, messages, **kwargs):
        raise AIUnavailable("boom", "SERVER")


async def _make_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_event(session) -> int:
    source_a = Source(name="Antara", base_url="https://www.antaranews.com")
    source_b = Source(name="CNN", base_url="https://www.cnnindonesia.com")
    session.add_all([source_a, source_b])
    await session.commit()

    event = Event(
        event_type=EventType.news,
        title="Berita utama hari ini",
        description="Berita tentang topik hari ini dengan detail ringkas.",
        status="active",
    )
    session.add(event)
    await session.commit()

    articles = [
        Article(
            source_id=source_a.id,
            title="Berita utama hari ini",
            url="https://www.antaranews.com/a",
            description="Ringkasan dari Antara.",
            content_hash="hash-a",
            published_at=datetime(2026, 9, 10, 12, 0, 0),
            processing_status=ProcessingStatus.raw,
        ),
        Article(
            source_id=source_b.id,
            title="Perkembangan berita hari ini",
            url="https://www.cnnindonesia.com/b",
            description="Update dari CNN.",
            content_hash="hash-b",
            published_at=datetime(2026, 9, 10, 12, 5, 0),
            processing_status=ProcessingStatus.raw,
        ),
    ]
    session.add_all(articles)
    await session.commit()

    session.add_all(
        [
            EventArticle(event_id=event.id, article_id=articles[0].id, relation_type=RelationType.primary, similarity_score=1.0),
            EventArticle(event_id=event.id, article_id=articles[1].id, relation_type=RelationType.related, similarity_score=0.6),
        ]
    )
    await session.commit()
    return event.id


async def test_wanted_tasks_by_event_type():
    assert wanted_tasks("news") == ["summary", "classification", "verification"]
    assert wanted_tasks("earthquake") == ["summary", "explanation"]
    assert "summary" in wanted_tasks("network")


async def test_process_event_persists_requests_and_interpretations():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_event(session)
        event = (
            await session.execute(select(Event).where(Event.id == event_id))
        ).scalars().first()
        completed = await process_event(session, event, FakeGateway())
        assert [item["interpretation_type"] for item in completed] == [
            "summary",
            "classification",
            "verification",
        ]

    async with factory() as session:
        requests = (await session.execute(select(AIRequest))).scalars().all()
        assert len(requests) == 3
        assert all(request.status == AIRequestStatus.succeeded for request in requests)
        assert all(request.input_tokens == 20 for request in requests)
        assert requests[0].key_name == "K1"

        interpretations = (
            await session.execute(select(AIInterpretation))
        ).scalars().all()
        assert len(interpretations) == 3
        assert interpretations[0].content == FAKE_JSON  # fences stripped
        assert interpretations[0].ai_request_id is not None


async def test_partial_failure_persists_summary_and_stays_pending():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_event(session)
        event = (
            await session.execute(select(Event).where(Event.id == event_id))
        ).scalars().first()
        gateway = FakeGateway(fail_after=2)
        with pytest.raises(AIUnavailable):
            await process_event(session, event, gateway)

    async with factory() as session:
        interpretations = (
            await session.execute(select(AIInterpretation))
        ).scalars().all()
        assert [item.interpretation_type for item in interpretations] == ["summary"]
        requests = (await session.execute(select(AIRequest))).scalars().all()
        assert requests[0].status == AIRequestStatus.succeeded
        assert requests[1].status == AIRequestStatus.failed
        assert requests[1].error_code == "EXHAUSTED"

        pending = await pick_pending_events(session, limit=5)
        assert [event.id for event in pending] == [event_id]


async def test_pick_pending_skips_fully_processed():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_event(session)
        event = (
            await session.execute(select(Event).where(Event.id == event_id))
        ).scalars().first()
        await process_event(session, event, FakeGateway())
        assert await pick_pending_events(session, limit=5) == []


async def test_full_failure_records_failed_request_and_no_interpretation():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_event(session)
        event = (
            await session.execute(select(Event).where(Event.id == event_id))
        ).scalars().first()
        with pytest.raises(AIUnavailable):
            await process_event(session, event, AlwaysFailsGateway())

    async with factory() as session:
        requests = (await session.execute(select(AIRequest))).scalars().all()
        assert len(requests) == 1
        assert requests[0].status == AIRequestStatus.failed
        assert requests[0].error_code == "SERVER"
        interpretations = (
            await session.execute(select(AIInterpretation))
        ).scalars().all()
        assert interpretations == []