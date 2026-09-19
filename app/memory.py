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

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Memory

KIND_ACTIONS = {"action", "note", "monitor", "cron", "preference"}


def _normalize_text(text: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace for fuzzy dedup."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_STOPWORDS = {
    "ada", "adalah", "akan", "apakah", "apa", "aku", "atau", "baik", "banyak",
    "baru", "bisa", "buat", "cuma", "dalam", "dan", "dari", "dengan",
    "dia", "di", "gak", "gua", "gw", "gue", "harus", "hari", "ini", "itu", "juga",
    "karena", "kayak", "ke", "kita", "kok", "lagi", "lah", "mau", "mereka", "nggak",
    "nanti", "orang", "pada", "para", "per", "pula", "saat", "saja", "sama", "saya",
    "sudah", "tapi", "terus", "the", "to", "and", "for", "with", "you", "your",
    "that", "this", "from", "not", "are", "was", "pakai", "untuk", "supaya",
}


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
    # Fuzzy dedup: exact OR normalized (case/punctuation-insensitive) match.
    norm = _normalize_text(content)
    if content in recent or any(_normalize_text(r) == norm for r in recent):
        return None

    session.add(Memory(content=content, kind=kind, source=source, meta=meta))
    await session.commit()

    # Transient action records (this log of what KELA did) age out instead of
    # permanently occupying the cap with one-liners.
    try:
        await prune_stale_actions(session)
    except Exception:  # noqa: BLE001
        pass

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


async def recall_kind(session: AsyncSession, kind: str, limit: int | None = None) -> list[Memory]:
    """Recall the most recent memories of one kind (e.g. ``monitor``)."""
    limit = limit or int(settings.memory_recall_limit or 10)
    limit = max(limit, 1)
    result = await session.execute(
        select(Memory)
        .where(Memory.kind == kind)
        .order_by(Memory.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", _normalize_text(text))
        if w not in _STOPWORDS and len(w) > 1
    }


def _score_memory(mem: Memory, query_tokens: set[str], now: datetime) -> float:
    """Keyword overlap (substring-aware) + recency decay."""
    content_norm = _normalize_text(mem.content)
    hits = sum(1 for t in query_tokens if t in content_norm)
    if hits == 0:
        return 0.0
    age_days = 0.0
    created = mem.created_at
    if created is not None:
        if created.tzinfo is not None:
            created = created.replace(tzinfo=None)
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    recency = 1.0 / (1.0 + age_days / 3.0)
    return hits * 2.0 + recency * 0.5


async def recall_relevant(
    session: AsyncSession,
    query: str,
    limit: int | None = None,
    pool: int = 200,
) -> list[Memory]:
    """Best-effort keyword recall: score recent memories against the user's
    current message so the injected block matches what they asked about.

    Falls back to plain newest-first recall when the query carries no usable
    tokens (or nothing matches), so the AI still sees *something* recent.
    """
    limit = limit or int(settings.memory_recall_limit or 10)
    limit = max(limit, 1)
    query_tokens = _tokens(query)
    if not query_tokens:
        return await recall(session, limit=limit)

    result = await session.execute(
        select(Memory).order_by(Memory.id.desc()).limit(max(pool, limit))
    )
    rows = list(result.scalars().all())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    scored = [(mem, _score_memory(mem, query_tokens, now)) for mem in rows]
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda pair: (-pair[1], -(pair[0].id or 0)))
    return [mem for mem, _ in scored[:limit]]


async def search_memories(session: AsyncSession, query: str, limit: int = 10) -> list[Memory]:
    """Explicit text search over memory contents (used by /memory cari ...)."""
    q = (query or "").strip()
    if not q:
        return []
    result = await session.execute(
        select(Memory)
        .where(Memory.content.ilike(f"%{q}%"))
        .order_by(Memory.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def memory_stats(session: AsyncSession) -> dict[str, int]:
    """Total memories plus a per-kind breakdown (for /memory header)."""
    total = await count(session)
    by_kind: dict[str, int] = {}
    result = await session.execute(
        select(Memory.kind, func.count(Memory.id)).group_by(Memory.kind)
    )
    for kind, n in result.all():
        by_kind[str(kind)] = int(n)
    return {"total": total, "by_kind": by_kind}


async def prune_stale_actions(session: AsyncSession, keep_days: int | None = None) -> int:
    """Drop transient ``action`` records older than ``MEMORY_ACTION_TTL_DAYS``.
    Notes/preferences/monitor naming stay until the global cap prunes them."""
    keep_days = keep_days if keep_days is not None else int(settings.memory_action_ttl_days or 30)
    if keep_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=keep_days)
    result = await session.execute(
        delete(Memory).where(
            Memory.kind == "action", Memory.created_at.is_not(None), Memory.created_at < cutoff
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Auto-detected user preferences ("gua suka X", "gua tinggal di Y", ...)
# ---------------------------------------------------------------------------

_PREF_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Ordered longest-first so "gak terlalu suka" wins over "gak suka".
    (
        re.compile(
            r"\b(?:gua|aku|saya|gw|gue)\s+(?P<verb>gak\s+terlalu\s+suka"
            r"|nggak\s+terlalu\s+suka|gak\s+suka|nggak\s+suka|gak\s+doyan"
            r"|nggak\s+doyan|gak\s+mau|nggak\s+mau|doyan\w*|biasanya\w*"
            r"|suka|biasa|sering|jarang|takut|pingin\w*|pengen\w*)"
            r"\s+(?P<rest>.+)$",
            re.IGNORECASE,
        ),
        "user {verb} {rest}",
    ),
    (
        re.compile(
            r"\b(?:gua|aku|saya|gw|gue)\s+(?:tinggal|domisili|diem|berada|base\w*)\s+"
            r"(?:di|di\s+sekitar)\s+(?P<rest>.+)$",
            re.IGNORECASE,
        ),
        "user tinggal di {rest}",
    ),
    (
        re.compile(
            r"\b(?:gua|aku|saya|gw|gue)\s+punya\s+(?P<rest>.+)$",
            re.IGNORECASE,
        ),
        "user punya {rest}",
    ),
    (
        re.compile(
            r"\b(?:gua|aku|saya|gw|gue)\s+nam(?:a|e)\w*\s+(?P<rest>.+)$",
            re.IGNORECASE,
        ),
        "user dipanggil {rest}",
    ),
    (
        re.compile(
            r"\bnam(?:a|e)\w*\s+(?:gua|aku|saya|gw|gue)\s+(?P<rest>.+)$",
            re.IGNORECASE,
        ),
        "user dipanggil {rest}",
    ),
    (
        re.compile(
            r"\b(?:panggil\w*|sapa\w*)\s+(?:gua|aku|saya|gw|gue)\s+(?P<rest>.+)",
            re.IGNORECASE,
        ),
        "user dipanggil {rest}",
    ),
]


def detect_preferences(text: str) -> list[str]:
    """Turn self-describing user statements into compact preference facts.

    Returns normalized strings like ``user suka kopi item``. Only statements
    *about the user* are captured — chit-chat / instructions / technical
    requests are left alone.
    """
    clean = re.sub(r"[.!?]+$", "", (text or "").strip())
    if not clean:
        return []
    out: list[str] = []
    for pattern, template in _PREF_PATTERNS:
        m = pattern.search(clean)
        if not m:
            continue
        rest = re.sub(r"\s+", " ", m.group("rest").strip(" ,.;:\t-"))
        if not rest or len(rest) < 2:
            continue
        fact = template.format(verb=(m.groupdict().get("verb") or "").strip(), rest=rest)
        if len(fact) < 4:
            continue
        if any(_normalize_text(fact) == _normalize_text(existing) for existing in out):
            continue
        out.append(fact)
    return out[:5]


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


def build_memory_section(
    memories: list[Memory], limit: int = 10, header: str | None = None
) -> str:
    """Compact bullet list for the AI system prompt (HTML-safe, fact-ordered)."""
    if not memories:
        return ""
    lines = [header or "Memori yang udah gua catet (kutip aja, jangan ngarang):"]
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


def format_memory_list(memories: list[Memory], stats: dict[str, int] | None = None) -> str:
    """Reply body for /memory — one card per row with an ID to forget."""
    if not memories:
        return "<b>Memory</b> — belum ada catatan."
    lines = ["<b>Memory (terbaru dulu)</b>"]
    if stats:
        by_kind = stats.get("by_kind") or {}
        breakdown = ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items()))
        if breakdown:
            lines.append(f"<i>{stats.get('total', 0)} catatan — {breakdown}</i>")
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