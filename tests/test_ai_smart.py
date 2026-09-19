"""Tests for the "smart AI" layer: conversation history, situational block,
host quick-overview, and free-text memory/scrape detection hooks.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.interfaces.context import (
    build_memory_section,
    build_situation_block,
    clear_history,
    get_history,
    get_summary,
    push_history,
    set_summary,
)
from app.interfaces.telegram import _detect_memory_request


@pytest.fixture(autouse=True)
def _redis_absent(monkeypatch):
    """Pin Redis off so shared live keys never leak into these unit tests."""
    from app.cache import get_redis as _get_redis
    from app.interfaces import context as _ctx

    monkeypatch.setattr(_ctx, "get_redis", lambda: None)
    monkeypatch.setattr("app.cache.get_redis", lambda: None)
    _ctx._history_fallback.clear()
    _ctx._summary_fallback.clear()
    yield


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Conversation history (Redis-absent in-memory fallback)
# ---------------------------------------------------------------------------

async def test_history_roundtrip_and_order():
    await push_history("c1", "halo", "hai bro")
    await push_history("c1", "gempa berapa", "M5.2 Maluku")
    rows = await get_history("c1")
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0]["content"] == "halo"


async def test_history_isolation_per_chat():
    await push_history("a", "satu", "1")
    await push_history("b", "dua", "2")
    assert [r["content"] for r in await get_history("a")] == ["satu", "1"]
    assert [r["content"] for r in await get_history("b")] == ["dua", "2"]


async def test_history_clear():
    await push_history("c", "x", "y")
    await clear_history("c")
    assert await get_history("c") == []


async def test_history_cap(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_chat_context_limit", 2)
    for i in range(5):
        await push_history("c", f"u{i}", f"a{i}")
    rows = await get_history("c")
    assert len(rows) == 4  # 2 exchanges * 2 roles
    assert rows[0]["content"] == "u3"


async def test_build_news_briefing_only_recent(session):
    from datetime import datetime, timedelta, timezone

    from app.db.models import Article, Source
    from app.interfaces.context import build_news_briefing

    src = Source(name="Antara", base_url="https://antaranews.com", feed_url="https://antaranews.com/rss", enabled=True)
    session.add(src)
    await session.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add_all(
        [
            Article(source_id=src.id, title="Berita baru hari ini", url="https://a/1", published_at=now - timedelta(hours=2)),
            Article(source_id=src.id, title="Berita basi 2025", url="https://a/2", published_at=datetime(2025, 7, 26, 10, 0)),
        ]
    )
    await session.commit()
    brief = await build_news_briefing(session, limit=5, max_hours=72)
    assert "Berita baru hari ini" in brief
    assert "Berita basi 2025" not in brief
    assert "Antara" in brief


# ---------------------------------------------------------------------------
# Situational-awareness block
# ---------------------------------------------------------------------------

async def test_build_situation_block_empty_db(session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_chat_context_limit", 2)
    out = await build_situation_block(session)
    assert isinstance(out, str)


async def test_build_situation_block_contains_host(session):
    out = await build_situation_block(session)
    if out:
        assert "memo" in out.lower() or "host" in out.lower()


# ---------------------------------------------------------------------------
# Host quick overview (reads /proc directly, no network)
# ---------------------------------------------------------------------------

async def test_quick_overview_shape():
    from app.hoststats import quick_overview

    out = await quick_overview()
    assert isinstance(out, str) and out.startswith("Host:")
    assert "RAM" in out


# ---------------------------------------------------------------------------
# Free-text memory detection
# ---------------------------------------------------------------------------

def test_detect_memory_request_inget():
    assert _detect_memory_request("inget server utama itu 10.70.1.1") == (
        "server utama itu 10.70.1.1"
    )


def test_detect_memory_request_catat():
    assert _detect_memory_request("catat password router diganti") == "password router diganti"


def test_detect_memory_request_not_ingetin_cron():
    assert _detect_memory_request("ingetin pulang jam 17.00") is None


def test_detect_memory_request_name_only():
    assert _detect_memory_request("inget") is None
    assert _detect_memory_request("catat   ") is None
    assert _detect_memory_request("tolong inget sama password") is None  # tidak match prefix


# ---------------------------------------------------------------------------
# Rolling long-term conversation summary (Redis-absent in-memory fallback)
# ---------------------------------------------------------------------------

async def test_summary_empty_by_default():
    assert await get_summary("ch") == ""


async def test_summary_roundtrip_and_cap(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_chat_summary", True)
    await set_summary("ch", "user lagi ngerjain config nginx di VPS Bekasi")
    assert "nginx" in await get_summary("ch")
    await set_summary("ch", "x" * (1500 + 10))
    assert len(await get_summary("ch")) <= 1400


def test_build_summary_block_content():
    from app.interfaces.context import build_summary_block

    assert build_summary_block("") == ""
    block = build_summary_block("user pindah ke Cikarang")
    assert "RINGKASAN" in block
    assert "Cikarang" in block


def test_build_memory_section_custom_header():
    rows = [type("M", (), {"content": "user suka matcha", "kind": "preference", "created_at": None})()]
    section = build_memory_section(rows, header="PREFERENSI USER:")
    assert section.startswith("PREFERENSI USER:")