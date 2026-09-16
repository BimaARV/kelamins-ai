"""Conversation history + situational-awareness block for the AI brain.

Two ideas that make the free-text bot feel like Jarvis:

  * ``push_history`` / ``get_history`` keep a short rolling conversation per
    chat in Redis (``bot:chat:<chat_id>``) so the model can follow up on the
    previous turns instead of forgetting everything between messages.
  * ``build_situation_block`` assembles a fresh 'what is happening right now'
    section (host snapshot, monitored targets + live status, active P1/P2
    alerts, top headlines, weather watch, persisted memory) that is injected
    into the system prompt on every AI turn — so answers come from facts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.config import settings
from app.db.models import (
    Alert,
    AlertPriority,
    Article,
    NetworkCheck,
    NetworkTarget,
    WeatherForecast,
    WeatherLocation,
)
from app.memory import build_memory_section, recall
from app.monitoring import list_monitored_ids

CHAT_KEY = "bot:chat:{}"
CHAT_TTL = 7 * 24 * 3600

_history_fallback: dict[str, list[dict]] = {}


def _cap() -> int:
    return max(int(settings.telegram_chat_context_limit or 8), 1) * 2


async def get_history(chat_id: str) -> list[dict]:
    cap = _cap()
    try:
        client = get_redis()
        if client is not None:
            raw = await client.get(CHAT_KEY.format(chat_id))
            rows = json.loads(raw) if raw else []
            return rows[-cap:]
    except Exception:  # noqa: BLE001
        pass
    return _history_fallback.get(str(chat_id), [])[-cap:]


async def push_history(chat_id: str, user_text: str, assistant_text: str) -> None:
    cap = _cap()
    entry = [
        {"role": "user", "content": user_text[:2000]},
        {"role": "assistant", "content": (assistant_text or "")[:3000]},
    ]
    try:
        client = get_redis()
        if client is not None:
            rows = await get_history(chat_id)
            rows = (rows + entry)[-cap:]
            await client.set(
                CHAT_KEY.format(chat_id), json.dumps(rows, ensure_ascii=False), ex=CHAT_TTL
            )
            return
    except Exception:  # noqa: BLE001
        pass
    _history_fallback[str(chat_id)] = (_history_fallback.get(str(chat_id), []) + entry)[-cap:]


async def clear_history(chat_id: str) -> None:
    try:
        client = get_redis()
        if client is not None:
            await client.delete(CHAT_KEY.format(chat_id))
            return
    except Exception:  # noqa: BLE001
        pass
    _history_fallback.pop(str(chat_id), None)


# ---------------------------------------------------------------------------
# Situation block
# ---------------------------------------------------------------------------


async def _weather_lines(session: AsyncSession, limit: int = 3) -> list[str]:
    locs = (
        await session.execute(
            select(WeatherLocation).where(WeatherLocation.enabled.is_(True)).order_by(WeatherLocation.id).limit(limit)
        )
    ).scalars().all()
    if not locs:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(hours=6)
    end = now + timedelta(hours=6)
    rows = (
        await session.execute(
            select(WeatherForecast, WeatherLocation)
            .join(WeatherLocation, WeatherForecast.location_id == WeatherLocation.id)
            .where(WeatherForecast.location_id.in_([l.id for l in locs]))
            .where(WeatherForecast.forecast_datetime >= start)
            .where(WeatherForecast.forecast_datetime <= end)
        )
    ).all()
    nearest: dict[int, tuple[WeatherForecast, WeatherLocation]] = {}
    for f, loc in rows:
        cur = nearest.get(loc.id)
        if cur is None or abs((f.forecast_datetime - now).total_seconds()) < abs(
            (cur[0].forecast_datetime - now).total_seconds()
        ):
            nearest[loc.id] = (f, loc)
    lines: list[str] = []
    for loc in locs:
        pair = nearest.get(loc.id)
        if pair is None:
            continue
        f, _ = pair
        temp = f"{f.temperature_c:.1f}C" if f.temperature_c is not None else "?"
        lines.append(f"  cuaca {loc.name}: {f.weather_description or 'n/a'} {temp}")
    return lines


async def build_situation_block(session: AsyncSession) -> str:
    """Fresh 'state of the world' section injected before each AI reply."""
    import logging

    logger = logging.getLogger("kela.context")
    lines: list[str] = []

    try:
        from app.hoststats import quick_overview

        lines.append("  " + await quick_overview())
    except Exception:  # noqa: BLE001
        logger.debug("quick_overview skipped", exc_info=True)

    try:
        ids = await list_monitored_ids()
        if ids:
            targets = (
                await session.execute(
                    select(NetworkTarget).where(NetworkTarget.id.in_(ids))
                )
            ).scalars().all()
            subq = (
                select(
                    NetworkCheck.target_id, func.max(NetworkCheck.id).label("max_id")
                )
                .where(NetworkCheck.target_id.in_(ids))
                .group_by(NetworkCheck.target_id)
                .subquery()
            )
            latest = (
                await session.execute(
                    select(NetworkCheck).join(subq, NetworkCheck.id == subq.c.max_id)
                )
            ).scalars().all()
            by_tid = {c.target_id: c for c in latest}
            for t in targets:
                c = by_tid.get(t.id)
                st = getattr(c.status, "value", c.status) if c else "belum dicek"
                lat = f" ({c.latency_ms:.0f} ms)" if c and c.latency_ms is not None else ""
                lines.append(f"  monitor {t.target}: {st}{lat}")
    except Exception:  # noqa: BLE001
        logger.debug("monitor block skipped", exc_info=True)

    try:
        alerts = (
            await session.execute(
                select(Alert)
                .where(Alert.priority.in_([AlertPriority.p1, AlertPriority.p2]))
                .order_by(desc(Alert.updated_at))
                .limit(3)
            )
        ).scalars().all()
        for a in alerts:
            state = getattr(a.status, "value", a.status) if a.status else "?"
            lines.append(f"  alert {a.priority.value if a.priority else '?'} {state}: {(a.title or '')[:120]}")
    except Exception:  # noqa: BLE001
        logger.debug("alert block skipped", exc_info=True)

    try:
        arts = (
            await session.execute(
                select(Article).order_by(desc(Article.scraped_at)).limit(5)
            )
        ).scalars().all()
        for a in arts:
            lines.append(f"  berita: {(a.title or '')[:100]}")
    except Exception:  # noqa: BLE001
        logger.debug("news block skipped", exc_info=True)

    try:
        lines.extend(await _weather_lines(session))
    except Exception:  # noqa: BLE001
        logger.debug("weather block skipped", exc_info=True)

    try:
        mem_block = build_memory_section(await recall(session))
        if mem_block:
            lines.append(mem_block)
    except Exception:  # noqa: BLE001
        logger.debug("memory block skipped", exc_info=True)

    if not lines:
        return ""
    return "\n".join(lines)