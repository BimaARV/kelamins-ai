"""Alert Engine tests: priority rules, one-thread-per-event lifecycle, channels."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import selectinload

from app.db.base import Base
from app.db.models import (
    Alert,
    AlertDelivery,
    AlertDeliveryStatus,
    AlertMessage,
    AlertMessageKind,
    AlertPriority,
    Article,
    Earthquake,
    Event,
    EventArticle,
    EventStatus,
    EventType,
    ProcessingStatus,
    RelationType,
    Source,
)
from app.alert_engine.channels import ChannelRegistry, deliver_message
from app.alert_engine.dedup import plan_alert
from app.alert_engine.priority import compute_priority, network_health
from app.alert_engine import run_alert_intelligence


async def _make_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_news_event(
    session, *, n_articles: int, n_sources: int = 1, event_type=EventType.news
) -> int:
    sources = [
        Source(name=f"Source-{i}", base_url=f"https://src{i}.example.com")
        for i in range(n_sources)
    ]
    session.add_all(sources)
    await session.commit()
    event = Event(event_type=event_type, title="Berita utama hari ini", status=EventStatus.active)
    session.add(event)
    await session.commit()
    articles = []
    for i in range(n_articles):
        source_id = sources[i % n_sources].id
        article = Article(
            source_id=source_id,
            title=f"Artikel {i} topik",
            url=f"https://src{i % n_sources}.example.com/{i}",
            description="Ringkasan.",
            content_hash=f"hash-{i}",
            published_at=datetime(2026, 9, 10, 12, 0, 0) + timedelta(minutes=i),
            processing_status=ProcessingStatus.raw,
        )
        session.add(article)
        articles.append(article)
    await session.commit()
    session.add_all(
        [
            EventArticle(
                event_id=event.id,
                article_id=a.id,
                relation_type=RelationType.primary,
                similarity_score=0.8,
            )
            for a in articles
        ]
    )
    await session.commit()
    return event.id


async def _stats_with(session, event_id: int) -> dict:
    from app.event_engine.confidence import event_stats

    event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
    return await event_stats(session, event)


async def test_priority_news_two_independent_sources_is_p2():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_news_event(session, n_articles=2, n_sources=2)
        event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
        stats = await _stats_with(session, event_id)
        priority, reason = await compute_priority(session, event, stats)
        assert priority == AlertPriority.p2
        assert "2 sumber independen" in reason


async def test_priority_news_single_source_is_p3():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_news_event(session, n_articles=1, n_sources=1)
        event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
        stats = await _stats_with(session, event_id)
        priority, _ = await compute_priority(session, event, stats)
        assert priority == AlertPriority.p3


async def test_priority_earthquake_magnitude_thresholds():
    factory = await _make_factory()
    async with factory() as session:
        event = Event(event_type=EventType.earthquake, title="Gempa", status=EventStatus.active)
        session.add(event)
        await session.commit()
        quake = Earthquake(
            event_id=event.id,
            external_id="ext-1",
            magnitude=5.2,
            depth_km=10.0,
            latitude=-7.0,
            longitude=126.0,
            place="Maluku Barat Daya",
            source="BMKG",
        )
        session.add(quake)
        await session.commit()

        priority, reason = await compute_priority(session, event, {})
        assert priority == AlertPriority.p1
        assert "signifikan" in reason


def test_network_priority_rules():
    from app.alert_engine.priority import _network_priority

    p1, reason = _network_priority({"total": 4, "down": 3})
    assert p1 == AlertPriority.p1
    assert "gangguan besar" in reason

    p2, _ = _network_priority({"total": 4, "down": 1})
    assert p2 == AlertPriority.p2

    p4, _ = _network_priority({"total": 4, "down": 0})
    assert p4 == AlertPriority.p4

    p4, _ = _network_priority({"total": 0, "down": 0})
    assert p4 == AlertPriority.p4


async def test_plan_alert_one_thread_lifecycle():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_news_event(session, n_articles=1, n_sources=1)
        event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
        stats = await _stats_with(session, event_id)

        priority, _ = await compute_priority(session, event, stats)
        assert priority == AlertPriority.p3

        action = await plan_alert(session, event, None, stats, priority, "x")
        assert action is not None
        assert action.kind == AlertMessageKind.initial

        alert = Alert(
            event_id=event.id,
            priority=priority,
            status=event.status.value,
            title=event.title,
        )
        session.add(alert)
        await session.commit()

        unchanged = await plan_alert(session, event, alert, stats, priority, "x")
        assert unchanged is None  # nothing changed -> no spam


async def test_plan_alert_severity_and_resolved_flow():
    factory = await _make_factory()
    async with factory() as session:
        event = Event(event_type=EventType.news, title="Event", status=EventStatus.active)
        session.add(event)
        await session.commit()
        stats = {"article_count": 0, "independent_source_count": 0, "spread_hours": None}
        priority = AlertPriority.p3

        action = await plan_alert(session, event, None, stats, priority, "x")
        assert action.kind == AlertMessageKind.initial

        alert = Alert(
            event_id=event.id,
            priority=priority,
            status=event.status.value,
            title=event.title,
            last_signature=action.signature,
        )
        session.add(alert)
        await session.commit()

        pump = {**stats, "independent_source_count": 2}
        pumped_priority = AlertPriority.p2
        action = await plan_alert(session, event, alert, pump, pumped_priority, "break")
        assert action is not None
        assert action.kind == AlertMessageKind.severity

        alert.priority = pumped_priority
        alert.last_signature = action.signature
        event.status = EventStatus.resolved
        await session.commit()

        action = await plan_alert(session, event, alert, pump, pumped_priority, "break")
        assert action is not None
        assert action.kind == AlertMessageKind.resolved

        alert.status = event.status.value
        alert.last_signature = action.signature
        await session.commit()

        again = await plan_alert(session, event, alert, pump, pumped_priority, "break")
        assert again is None  # resolved already notified


async def test_update_requires_new_articles_or_sources():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_news_event(session, n_articles=3, n_sources=1)
        event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
        stats = await _stats_with(session, event_id)
        priority = AlertPriority.p3
        action = await plan_alert(session, event, None, stats, priority, "x")
        assert action.kind == AlertMessageKind.initial

        alert = Alert(
            event_id=event.id,
            priority=priority,
            status="active",
            title=event.title,
            last_signature=action.signature,
        )
        session.add(alert)
        await session.commit()

        # +2 same-source articles: indep unchanged, +2 articles (< min_new_articles 5)
        await _append_articles(session, event_id, count=2, n_sources=1)
        stats2 = await _stats_with(session, event_id)
        action = await plan_alert(session, event, alert, stats2, priority, "x")
        assert action is None  # below thresholds

        # +5 articles -> update fires (with cooled-down window)
        alert.last_sent_at = datetime.utcnow() - timedelta(hours=1)
        await session.commit()
        await _append_articles(session, event_id, count=5, n_sources=1)
        stats3 = await _stats_with(session, event_id)
        action = await plan_alert(session, event, alert, stats3, priority, "x")
        assert action is not None
        assert action.kind == AlertMessageKind.update


async def _append_articles(session, event_id: int, *, count: int, n_sources: int) -> None:
    sources = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
    now_max = (await session.execute(select(Article).order_by(Article.id.desc()))).scalars().first()
    start = int(now_max.id if now_max else 0) + 1
    articles = []
    for i in range(start, start + count):
        source_id = sources[i % len(sources)].id
        article = Article(
            source_id=source_id,
            title=f"Artikel baru {i}",
            url=f"https://src{i % len(sources)}.example.com/art/{i}",
            description="Update.",
            content_hash=f"hash-new-{i}",
            published_at=datetime(2026, 9, 10, 13, 0, 0) + timedelta(minutes=i),
            processing_status=ProcessingStatus.raw,
        )
        session.add(article)
        articles.append(article)
    await session.commit()
    session.add_all(
        [
            EventArticle(
                event_id=event.id,
                article_id=a.id,
                relation_type=RelationType.related,
                similarity_score=0.7,
            )
            for a in articles
        ]
    )
    await session.commit()


async def test_plan_alert_ignores_closed_events_without_alert():
    factory = await _make_factory()
    async with factory() as session:
        event = Event(event_type=EventType.news, title="Y", status=EventStatus.resolved)
        session.add(event)
        await session.commit()
        action = await plan_alert(
            session, event, None, {}, AlertPriority.p3, "x"
        )
        assert action is None


async def test_network_health_empty_targets():
    factory = await _make_factory()
    async with factory() as session:
        assert await network_health(session) == {"total": 0, "up": 0, "down": 0}


async def test_no_channels_yields_skipped_delivery(monkeypatch):
    factory = await _make_factory()
    async with factory() as session:
        event = Event(event_type=EventType.news, title="E", status=EventStatus.active)
        session.add(event)
        await session.commit()
        alert = Alert(event_id=event.id, priority=AlertPriority.p1, title="E")
        session.add(alert)
        await session.commit()
        message = AlertMessage(
            alert_id=alert.id,
            kind=AlertMessageKind.initial,
            priority=AlertPriority.p1,
            title="E",
            body="body",
        )
        session.add(message)
        await session.commit()

        registry = ChannelRegistry.from_settings(
            type("S", (), {
                "telegram_bot_token": None,
                "telegram_chat_id": None,
                "discord_webhook_url": None,
            })()
        )
        assert registry.channels() == ["none"]
        assert not registry.enabled()
        results = await deliver_message(session, message, registry)
        assert results == [
            {"channel": "none", "status": AlertDeliveryStatus.skipped, "error": "no channel configured"}
        ]


async def test_telegram_and_discord_delivery_failures(monkeypatch):
    factory = await _make_factory()
    async with factory() as session:
        event = Event(event_type=EventType.news, title="E", status=EventStatus.active)
        session.add(event)
        await session.commit()
        alert = Alert(event_id=event.id, priority=AlertPriority.p1, title="E")
        session.add(alert)
        await session.commit()
        message = AlertMessage(
            alert_id=alert.id,
            kind=AlertMessageKind.initial,
            priority=AlertPriority.p1,
            title="E",
            body="body",
        )
        session.add(message)
        await session.commit()

        registry = ChannelRegistry(
            telegram_token="TOKEN", telegram_chat_id="123", discord_webhook_url="https://x"
        )
        from app.alert_engine import channels as channels_mod

        async def fake_telegram(token, chat_id, text):
            del token, chat_id, text
            return "msg:1"

        async def fake_discord(url, text, username="KELA Alert"):
            del url, text, username
            return ""

        async def fake_discord_fail(url, text, username="KELA Alert"):
            del url, text, username
            raise ValueError("rate limited")

        monkeypatch.setattr(channels_mod, "_send_telegram", fake_telegram)
        monkeypatch.setattr(channels_mod, "_send_discord", fake_discord)
        results = await deliver_message(session, message, registry)
        assert results[0] == {"channel": "telegram", "status": AlertDeliveryStatus.delivered, "external_id": "msg:1"}
        assert results[1] == {"channel": "discord", "status": AlertDeliveryStatus.delivered, "external_id": ""}

        monkeypatch.setattr(channels_mod, "_send_discord", fake_discord_fail)
        results = await deliver_message(session, message, registry)
        assert results[1]["status"] == AlertDeliveryStatus.failed
        assert "rate limited" in results[1]["error"]


async def test_run_alert_intelligence_creates_one_alert_and_tracks():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_news_event(session, n_articles=1, n_sources=1, event_type=EventType.other)
        result = await run_alert_intelligence(
            session, registry=ChannelRegistry(None, None, None)
        )
        assert result["created"] == 1
        assert result["messages_sent"] == 1
        assert result["deliveries_sent"] == 1  # "none" skipped delivery recorded

        alert = (await session.execute(select(Alert))).scalars().one()
        assert alert.event_id == event_id
        assert alert.priority == AlertPriority.p3
        assert alert.message_count == 1
        assert alert.last_signature is not None

        message = (
            await session.execute(
                select(AlertMessage).options(selectinload(AlertMessage.deliveries))
            )
        ).scalars().one()
        assert message.kind == AlertMessageKind.initial
        assert message.priority == AlertPriority.p3
        assert "Sumber: " in message.body
        assert message.deliveries[0].channel == "none"
        assert message.deliveries[0].status == AlertDeliveryStatus.skipped

        # second cycle: nothing changed -> no new messages
        result = await run_alert_intelligence(
            session, registry=ChannelRegistry(None, None, None)
        )
        assert result["messages_sent"] == 0


async def test_run_alert_intelligence_resolved_and_update_lifecycle():
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_news_event(session, n_articles=1, n_sources=1, event_type=EventType.other)
        await run_alert_intelligence(
            session, registry=ChannelRegistry(None, None, None)
        )

        event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
        event.status = EventStatus.resolved
        await session.commit()

        result = await run_alert_intelligence(
            session, registry=ChannelRegistry(None, None, None)
        )
        message = (
            await session.execute(select(AlertMessage).order_by(AlertMessage.id.desc()))
        ).scalars().first()
        assert message.kind == AlertMessageKind.resolved
        assert message.priority == AlertPriority.p4
        assert result["messages_sent"] == 1

        # resolved already notified, run again -> quiet
        result = await run_alert_intelligence(
            session, registry=ChannelRegistry(None, None, None)
        )
        assert result["messages_sent"] == 0


async def test_run_alert_intelligence_skips_already_notified_resolved():
    factory = await _make_factory()
    async with factory() as session:
        event = Event(event_type=EventType.other, title="E", status=EventStatus.resolved)
        session.add(event)
        await session.commit()
        session.add(
            Alert(event_id=event.id, priority=AlertPriority.p3, status=EventStatus.resolved, title="E",
                  last_signature=f"P3|resolved|0|0")
        )
        await session.commit()
        result = await run_alert_intelligence(
            session, registry=ChannelRegistry(None, None, None)
        )
        assert result["messages_sent"] == 0


async def test_run_alert_intelligence_ignores_news_events():
    """Berita hanya muncul via /news & /news-tech — tidak boleh auto-alert."""
    factory = await _make_factory()
    async with factory() as session:
        event_id = await _make_news_event(session, n_articles=3, n_sources=2)
        event = (await session.execute(select(Event).where(Event.id == event_id))).scalars().first()
        assert event.event_type == EventType.news

        result = await run_alert_intelligence(
            session, registry=ChannelRegistry("TOKEN", "123", "https://x")
        )
        assert result["created"] == 0
        assert result["messages_sent"] == 0
        assert result["deliveries_sent"] == 0
        assert (await session.execute(select(Alert))).scalars().first() is None