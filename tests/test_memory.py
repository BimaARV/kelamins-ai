"""Tests for the Jarvis memory layer (app/memory.py)."""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.memory import (
    build_memory_section,
    count,
    forget,
    format_memory_list,
    recall,
    remember,
)


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


async def test_remember_and_recall(session):
    mem = await remember(session, "user suka kopi item tanpa gula", kind="note")
    assert mem is not None
    assert mem.kind == "note"
    rows = await recall(session)
    assert len(rows) == 1
    assert rows[0].content == "user suka kopi item tanpa gula"


async def test_remember_dedup_within_window(session):
    await remember(session, "janji meeting jam 10", kind="note")
    second = await remember(session, "janji meeting jam 10", kind="note")
    assert second is None
    assert await count(session) == 1


async def test_remember_kind_whitelist(session):
    mem = await remember(session, "raw action", kind="weird")
    assert mem is not None
    assert mem.kind == "note"
    mem = await remember(session, "something important", kind="action")
    assert mem.kind == "action"


async def test_remember_empty_content(session):
    assert await remember(session, "  ") is None
    assert await remember(session, "") is None


async def test_forget(session):
    mem = await remember(session, "hapus ini", kind="note")
    assert await forget(session, mem.id) is True
    assert await forget(session, mem.id) is False
    assert await count(session) == 0


async def test_cap_prunes_oldest(session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "memory_max_items", 55)
    for i in range(60):
        await remember(session, f"note #{i}", kind="note")
    assert await count(session) == 55
    rows = await recall(session, limit=60)
    contents = {r.content for r in rows}
    assert "note #0" not in contents
    assert "note #59" in contents


def test_build_memory_section_empty():
    assert build_memory_section([]) == ""


def test_build_memory_section_bullets(session):
    rows = [type("M", (), {"content": "a", "kind": "note", "created_at": None})()]
    section = build_memory_section(rows)
    assert "[note]" in section
    assert "a" in section
    assert "•" in section


def test_format_memory_list_empty():
    assert "belum ada catatan" in format_memory_list([])


def test_format_memory_list_cards(session):
    rows = [type("M", (), {"id": 5, "content": "x", "kind": "action", "created_at": None})()]
    out = format_memory_list(rows)
    assert "#5" in out
    assert "/forget" in out
    assert "x" in out