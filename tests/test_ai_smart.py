"""Tests for the "smart AI" layer: conversation history, situational block,
host quick-overview, and free-text memory/scrape detection hooks.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.interfaces.context import build_situation_block, clear_history, get_history, push_history
from app.interfaces.telegram import _detect_memory_request


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