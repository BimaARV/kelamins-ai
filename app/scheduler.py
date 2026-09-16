"""KELA scheduler - runs scheduled collectors in its own process.

Separation from the API means restarting the API never stops data collection.
Collectors store RAW data only; AI processing happens later and never blocks a
collection cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.cache import set_heartbeat
from app.alert_engine import run_alert_intelligence
from app.collectors.earthquake.bmkg import fetch_latest_earthquake
from app.collectors.network.checks import run_check
from app.collectors.news.rss import fetch_and_parse_feed
from app.collectors.weather.bmkg import fetch_forecast, parse_weather_payload
from app.config import settings
from app.db.repo import (
    list_enabled_sources,
    list_enabled_targets,
    list_enabled_weather_locations,
    seed_if_empty,
    seed_weather_locations_if_empty,
    store_network_check,
    store_raw_articles,
    sync_source_fixtures,
    sync_weather_locations_if_missing,
    update_weather_location,
    upsert_earthquake,
    upsert_weather_forecasts,
)
from app.db.session import session_factory
from app.event_engine import run_event_intelligence
from app.kela_ai.gateway import AIUnavailable, build_gateway
from app.kela_ai.services import pick_pending_events, process_event
from app.monitoring import maybe_notify_monitor

logger = logging.getLogger("kela.scheduler")


async def _loop_news() -> None:
    while True:
        try:
            async with session_factory() as session:
                sources = await list_enabled_sources(session)
                for source in sources:
                    if not source.feed_url:
                        continue
                    try:
                        items = await fetch_and_parse_feed(
                            source.feed_url, source.id, settings.http_timeout_seconds
                        )
                        stored = await store_raw_articles(session, items)
                        logger.info(
                            "news collected source=%s feed=%s items=%d stored=%d",
                            source.name, source.feed_url, len(items), stored,
                        )
                    except Exception as exc:  # noqa: BLE001 - one failed source never kills polling
                        logger.warning("news collector failed source=%s: %s", source.name, exc)
                await set_heartbeat(
                    "scheduler:last_news_at", datetime.now(timezone.utc).isoformat()
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("news loop error: %s", exc)
        await asyncio.sleep(settings.news_poll_interval_seconds)


async def _loop_earthquake() -> None:
    while True:
        try:
            async with session_factory() as session:
                fact = await fetch_latest_earthquake(
                    settings.bmkg_feed_url, settings.http_timeout_seconds
                )
                created = await upsert_earthquake(session, fact)
                await set_heartbeat(
                    "scheduler:last_earthquake_at", datetime.now(timezone.utc).isoformat()
                )
                if fact:
                    logger.info(
                        "earthquake stored new=%s mag=%s place=%s at=%s",
                        created, fact.get("magnitude"), fact.get("place"),
                        fact.get("occurred_at"),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("earthquake collector failed: %s", exc)
        await asyncio.sleep(settings.earthquake_poll_interval_seconds)


async def _loop_network() -> None:
    last_run: dict[int, float] = {}
    while True:
        try:
            async with session_factory() as session:
                targets = await list_enabled_targets(session)
                now = datetime.now(timezone.utc).timestamp()
                for target in targets:
                    due = last_run.get(target.id, 0.0) + (target.interval_seconds or 60)
                    if now < due:
                        continue
                    result = await run_check(target)
                    await store_network_check(session, target.id, result)
                    last_run[target.id] = now
                    if await maybe_notify_monitor(target, result):
                        logger.info(
                            "monitor transition alert sent target=%s status=%s",
                            target.name, result["status"],
                        )
                    logger.info(
                        "network check done target=%s status=%s latency=%s err=%s",
                        target.name, result["status"], result.get("latency_ms"),
                        result.get("error_message"),
                    )
                await set_heartbeat(
                    "scheduler:last_network_at", datetime.now(timezone.utc).isoformat()
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("network loop error: %s", exc)
        await asyncio.sleep(settings.network_poll_interval_seconds)


async def _loop_weather() -> None:
    while True:
        try:
            async with session_factory() as session:
                locations = await list_enabled_weather_locations(session)
                for location in locations:
                    try:
                        payload = await fetch_forecast(
                            location.adm4,
                            timeout=settings.http_timeout_seconds,
                            endpoint=settings.bmkg_weather_endpoint,
                        )
                        info, rows = parse_weather_payload(payload)
                        if info.get("adm4"):
                            await update_weather_location(session, location, info)
                        if rows:
                            count = await upsert_weather_forecasts(session, location.id, rows)
                            logger.info(
                                "weather stored location=%s adm4=%s rows=%d",
                                location.name, location.adm4, count,
                            )
                    except Exception as exc:  # noqa: BLE001 - one location never kills loop
                        logger.warning("weather collector failed location=%s: %s", location.name, exc)
                await set_heartbeat(
                    "scheduler:last_weather_at", datetime.now(timezone.utc).isoformat()
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("weather loop error: %s", exc)
        await asyncio.sleep(settings.weather_poll_interval_seconds)


async def _loop_events() -> None:
    while True:
        try:
            async with session_factory() as session:
                result = await run_event_intelligence(session)
                await set_heartbeat(
                    "scheduler:last_events_at", datetime.now(timezone.utc).isoformat()
                )
                if result.get("changed"):
                    logger.info("event engine cycle %s", result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("event loop error: %s", exc)
        await asyncio.sleep(settings.event_poll_interval_seconds)


async def _loop_ai() -> None:
    gateway = build_gateway(settings)
    if not gateway.client:
        logger.error("ai gateway could not be built - ai loop disabled")
        return
    consecutive_failures = 0
    while True:
        try:
            async with session_factory() as session:
                pending = await pick_pending_events(
                    session, limit=settings.ai_max_events_per_cycle
                )
                if not pending:
                    await set_heartbeat(
                        "scheduler:last_ai_at", datetime.now(timezone.utc).isoformat()
                    )
                    if not gateway.configured:
                        await set_heartbeat("scheduler:ai_state", "no_keys")
                    await asyncio.sleep(settings.ai_poll_interval_seconds)
                    continue
                for event in pending:
                    try:
                        completed = await process_event(session, event, gateway)
                        logger.info(
                            "ai processed event=%s tasks=%s result=%s",
                            event.id,
                            ",".join(item["interpretation_type"] for item in completed),
                            completed[0]["content"][:40] if completed else "",
                        )
                    except AIUnavailable as exc:
                        consecutive_failures += 1
                        logger.warning(
                            "ai unavailable for event=%s (%s) - %d in a row",
                            event.id, exc.error_code, consecutive_failures,
                        )
                        break
                    consecutive_failures = 0
                await set_heartbeat(
                    "scheduler:last_ai_at", datetime.now(timezone.utc).isoformat()
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("ai loop error: %s", exc)
        if consecutive_failures >= 6:
            await set_heartbeat("scheduler:ai_state", "unavailable")
        await asyncio.sleep(settings.ai_poll_interval_seconds)


async def _loop_alerts() -> None:
    while True:
        try:
            async with session_factory() as session:
                result = await run_alert_intelligence(session)
                await set_heartbeat(
                    "scheduler:last_alerts_at", datetime.now(timezone.utc).isoformat()
                )
                if result.get("messages_sent"):
                    logger.info("alert loop cycle %s", result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("alert loop error: %s", exc)
        await asyncio.sleep(settings.alert_poll_interval_seconds)


async def _heartbeat_tick() -> None:
    while True:
        await set_heartbeat("scheduler:last_tick", datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(30)


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    async with session_factory() as session:
        await seed_if_empty(session)
        await sync_source_fixtures(session)
        await seed_weather_locations_if_empty(session)
        await sync_weather_locations_if_missing(session)

    logger.info("kela scheduler starting (env=%s)", settings.app_env)
    await asyncio.gather(
        _loop_news(),
        _loop_earthquake(),
        _loop_network(),
        _loop_weather(),
        _loop_events(),
        _loop_ai(),
        _loop_alerts(),
        _heartbeat_tick(),
    )


if __name__ == "__main__":
    asyncio.run(main())