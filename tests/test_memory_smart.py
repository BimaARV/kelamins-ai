"""Tests for the smart-memory layer: relevant recall, auto-detected
preferences, near-duplicate dedup, stats/search, and stale-action pruning.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Memory
from app.memory import (
    build_memory_section,
    count,
    detect_preferences,
    format_memory_list,
    memory_stats,
    prune_stale_actions,
    recall,
    recall_relevant,
    remember,
    search_memories,
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


async def _add(session, content, kind="note", age_days=0.0):
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    session.add(Memory(content=content, kind=kind, source="test", created_at=created))
    await session.flush()


# ---------------------------------------------------------------------------
# Relevant recall (keyword + recency scoring)
# ---------------------------------------------------------------------------

async def test_recall_relevant_matches_query_substring(session):
    await _add(session, "user punya server di Bekasi", age_days=10)
    await _add(session, "rencana liburan ke Bali", age_days=0)
    rows = await recall_relevant(session, "server gua di mana ya")
    assert "server" in rows[0].content
    assert "Bali" not in rows[0].content


async def test_recall_relevant_recency_tiebreak(session):
    await _add(session, "server lama mati", age_days=5)
    await _add(session, "server baru aja up", age_days=0)
    rows = await recall_relevant(session, "server")
    assert rows[0].content == "server baru aja up"


async def test_recall_relevant_falls_back_to_newest(session):
    await _add(session, "janji meeting jam 10", age_days=0)
    await _add(session, "cek berkala tiap jam", age_days=1)
    rows = await recall_relevant(session, "gua kok")  # all stopwords -> no tokens
    assert rows[0].content == "cek berkala tiap jam"  # newest by id


async def test_recall_relevant_preference_kind(session):
    await _add(session, "user suka ngoding", kind="preference", age_days=0)
    rows = await recall_relevant(session, "suka apa gua ya")
    assert rows and rows[0].kind == "preference"


# ---------------------------------------------------------------------------
# Preference auto-detection
# ---------------------------------------------------------------------------

def test_detect_preferences_verb():
    assert "user suka kopi item tanpa gula" in detect_preferences("gua suka kopi item tanpa gula")
    assert "user nggak suka pedas" in detect_preferences("aku nggak suka pedas")


def test_detect_preferences_location():
    assert "user tinggal di Bekasi" in detect_preferences("saya tinggal di Bekasi")
    assert "user tinggal di sekitar Cikarang" in detect_preferences("saya tinggal di sekitar Cikarang")


def test_detect_preferences_possession_and_name():
    assert "user punya 2 vps di rumah" in detect_preferences("gue punya 2 vps di rumah")
    assert "user dipanggil Bima" in detect_preferences("nama gua Bima")
    assert "user dipanggil Ajay" in detect_preferences("panggil gua Ajay")


def test_detect_preferences_ignores_requests():
    assert detect_preferences("monitor 8.8.8.8 dong") == []
    assert detect_preferences("cron besok pagi ingetin meeting") == []
    assert detect_preferences("tolong trace ke BIOS") == []


# ---------------------------------------------------------------------------
# Dedup, stats, search, prune
# ---------------------------------------------------------------------------

async def test_remember_near_duplicate_dedup(session):
    await remember(session, "User Suka Kopi")
    second = await remember(session, "user suka kopi")
    assert second is None
    assert await count(session) == 1


async def test_remember_allows_preference_kind(session):
    mem = await remember(session, "user suka ngoding", kind="preference")
    assert mem is not None and mem.kind == "preference"


async def test_memory_stats_breakdown(session):
    await remember(session, "catatan aja", kind="note")
    await remember(session, "user suka teh", kind="preference")
    stats = await memory_stats(session)
    assert stats["total"] == 2
    assert stats["by_kind"]["note"] == 1
    assert stats["by_kind"]["preference"] == 1


async def test_search_memories_text(session):
    await _add(session, "monitor RO-BIOS pakai 10.10.70.1")
    await _add(session, "inget beli susu")
    rows = await search_memories(session, "bios")
    assert len(rows) == 1 and "RO-BIOS" in rows[0].content
    assert await search_memories(session, "  ") == []


async def test_prune_stale_actions_only_old_actions(session):
    await _add(session, "scrape sukses lama", kind="action", age_days=40)
    await _add(session, "scrape sukses baru", kind="action", age_days=1)
    await _add(session, "note lama tapi penting", kind="note", age_days=40)

    removed = await prune_stale_actions(session, keep_days=30)
    assert removed == 1

    rows = await recall(session, limit=10)
    contents = [r.content for r in rows]
    assert "scrape sukses lama" not in contents
    assert "scrape sukses baru" in contents
    assert "note lama tapi penting" in contents


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

async def test_format_memory_list_stats_header(session):
    await remember(session, "satu", kind="note")
    stats = await memory_stats(session)
    rows = await recall(session, limit=1)
    out = format_memory_list(rows, stats=stats)
    assert "catatan" in out and "note" in out


def test_build_memory_section_custom_header():
    rows = [type("M", (), {"content": "x", "kind": "preference", "created_at": None})()]
    assert build_memory_section(rows, header="PREFERENSI USER").startswith("PREFERENSI USER")