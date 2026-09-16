"""Jarvis-style persistent memory for the KELA assistant.

Memory rounds out the free-text bot: important actions (monitoring start/stop,
down/up flips, cron, whois, file generation) and explicit user notes
('inget/catat/hafal …') land in the ``memories`` table, then get injected into
the AI system prompt on every free-text turn so the assistant can say 'yes,
you asked me to monitor that — here's the current state' instead of guessing.

Rules:
  * Fact-first: memory rows are raw text; the AI may only *quote* them.
  * Dedup: identical content stored within the last ``MEMORY_DEDUP_WINDOW``
    rows is skipped so a repeating scheduler cycle never spams the table.
  * Cap: oldest rows are pruned when the table passes ``MEMORY_MAX_ITEMS``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Memory

KIND_ACTIONS = {"action", "note", "monitor", "cron"}


async def remember(
    session: AsyncSession,
    content: str,
    *,
    kind: str = "note",
    source: str = "telegram",
    meta: dict | None = None,
) -> Memory | None:
    """Store a memory row. Returns None when the content is a duplicate."""
    content = (content or "").strip()
    if len(content) < 2:
        return None
    kind = kind if kind in KIND_ACTIONS else "note"

    recent = (
        await session.execute(
            select(Memory.content).order_by(Memory.id.desc()).limit(50)
        )
    ).scalars().all()
    if content in recent:
        return None

    session.add(Memory(content=content, kind=kind, source=source, meta=meta))
    await session.commit()

    count = (await session.execute(select(func.count(Memory.id)))).scalar() or 0
    max_items = max(int(settings.memory_max_items or 200), 50)
    if count > max_items:
        too_many = count - max_items
        old_ids = (
            await session.execute(
                select(Memory.id).order_by(Memory.id.asc()).limit(too_many)
            )
        ).scalars().all()
        if old_ids:
            await session.execute(delete(Memory).where(Memory.id.in_(old_ids)))
            await session.commit()
    return (
        await session.execute(
            select(Memory).order_by(Memory.id.desc()).limit(1)
        )
    ).scalars().first()


async def recall(session: AsyncSession, limit: int | None = None) -> list[Memory]:
    limit = limit or int(settings.memory_recall_limit or 10)
    limit = max(limit, 1)
    result = await session.execute(
        select(Memory).order_by(Memory.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def forget(session: AsyncSession, memory_id: int) -> bool:
    row = (
        await session.execute(select(Memory).where(Memory.id == memory_id))
    ).scalars().first()
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def count(session: AsyncSession) -> int:
    value = await session.execute(select(func.count(Memory.id)))
    return int(value.scalar() or 0)


def build_memory_section(memories: list[Memory], limit: int = 10) -> str:
    """Compact bullet list for the AI system prompt (HTML-safe, fact-ordered)."""
    if not memories:
        return ""
    lines = ["Memori yang udah gua catet (kutip aja, jangan ngarang):"]
    for mem in memories[:limit]:
        when = mem.created_at
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        label = ""
        if when is not None:
            try:
                local = when.astimezone()
                label = f" [{local:%d/%m %H:%M}]"
            except (ValueError, OverflowError):
                label = ""
        lines.append(f"  • [{mem.kind}]{label} {mem.content}")
    return "\n".join(lines)


def format_memory_list(memories: list[Memory]) -> str:
    """Reply body for /memory — one card per row with an ID to forget."""
    if not memories:
        return "<b>Memory</b> — belum ada catatan."
    lines = ["<b>Memory (terbaru dulu)</b>"]
    for mem in memories:
        when = mem.created_at
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        ts = ""
        if when is not None:
            try:
                ts = f" · {when.astimezone():%a, %d %b %Y, %H:%M}"
            except (ValueError, OverflowError):
                ts = ""
        lines.append("")
        lines.append(f"<b>#{mem.id}</b> [{mem.kind}]{ts}")
        lines.append(f"<code>{mem.content}</code>")
    lines.append("")
    lines.append("Hapus: /forget &lt;id&gt;")
    return "\n".join(lines)