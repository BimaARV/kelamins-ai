"""Redis client for observability/queue/cache. Redis is optional at runtime."""

from __future__ import annotations

import json
import logging

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis | None:
    """Lazily build the shared client. Returns None on unreachable Redis."""
    global _client
    try:
        if _client is None:
            _client = aioredis.from_url(settings.redis_url, decode_responses=True)
            _client.close = lambda: None  # keep one client for app lifetime
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis init failed: %s", exc)
        return None
    return _client


async def redis_available() -> bool:
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception:  # noqa: BLE001
        return False


async def set_heartbeat(key: str, value: object) -> None:
    client = get_redis()
    if client is None:
        return
    try:
        payload = json.dumps(value, default=str) if not isinstance(value, str) else value
        await client.set(key, payload, ex=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis set failed for %s: %s", key, exc)


async def get_heartbeat(key: str) -> str | None:
    client = get_redis()
    if client is None:
        return None
    try:
        return await client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis get failed for %s: %s", key, exc)
        return None