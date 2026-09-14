"""Alert Engine (Phase 4) - event-centric notification pipeline.

Everything is fact-driven and AI-free so alerts stay deterministic and
traceable: P1/P2 decisions never depend on an LLM being online.
"""

from app.alert_engine.channels import ChannelRegistry, deliver_message
from app.alert_engine.dedup import AlertAction, plan_alert
from app.alert_engine.messages import build_alert_body
from app.alert_engine.priority import compute_priority, network_health

__all__ = [
    "AlertAction",
    "ChannelRegistry",
    "compute_priority",
    "build_alert_body",
    "network_health",
    "plan_alert",
    "deliver_message",
    "run_alert_intelligence",
]

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Alert, AlertMessageKind, AlertPriority, Event, EventType
from app.db.repo import add_alert_delivery, add_alert_message, get_alert_for_event
from app.event_engine.confidence import event_stats

logger = logging.getLogger(__name__)


async def run_alert_intelligence(session: AsyncSession, registry: ChannelRegistry | None = None) -> dict:
    """Process events into alert threads (dedup per event) and deliver.

    One alert per event. Initial alerts are created for every non-closed event
    without one yet; updates/severity/resolved notes fire only when the event
    actually changed and the cooldown has elapsed. Events without an alert are
    prioritized so no event starves under a fixed per-cycle budget.

    ``EventType.news`` events are skipped entirely — news lives on-demand via
    the ``/news`` and ``/news-tech`` commands, so it must never auto-notify.
    """
    if registry is None:
        registry = ChannelRegistry.from_settings(settings)
    capacity = max(settings.alert_max_events_per_cycle, 1)
    missing_alerts = (
        select(Event)
        .where(~select(Alert.id).where(Alert.event_id == Event.id).exists())
        .order_by(Event.updated_at.desc())
        .limit(capacity)
    )
    with_alerts = (
        select(Event)
        .where(select(Alert.id).where(Alert.event_id == Event.id).exists())
        .order_by(Event.updated_at.desc())
        .limit(capacity)
    )
    events = list((await session.execute(missing_alerts)).scalars().all())
    events += list((await session.execute(with_alerts)).scalars().all())
    events = [e for e in events if e.event_type != EventType.news]

    created = 0
    messages_sent = 0
    deliveries_sent = 0
    for event in events:
        alert = await get_alert_for_event(session, event.id)
        stats = await event_stats(session, event)
        priority, reason = await compute_priority(
            session, event, stats, network_state=await network_health(session)
        )
        action = await plan_alert(session, event, alert, stats, priority, reason)
        if action is None:
            continue

        if alert is None:
            alert = Alert(
                event_id=event.id,
                priority=priority,
                status=event.status.value,
                title=event.title,
            )
            session.add(alert)
            await session.commit()
            created += 1

        message_priority = _message_priority(alert, action.kind, priority)
        message = await add_alert_message(
            session,
            alert_id=alert.id,
            kind=action.kind,
            priority=message_priority,
            title=event.title,
            body=await build_alert_body(
                session, event, stats, message_priority, action.kind, reason
            ),
            meta={
                "article_count": stats["article_count"],
                "independent_sources": stats["independent_source_count"],
                "reason": reason,
                "action": action.kind,
            },
        )
        alert.message_count += 1
        alert.last_signature = action.signature
        alert.last_sent_at = message.created_at
        if action.kind in (AlertMessageKind.initial, AlertMessageKind.severity):
            alert.priority = priority
        alert.status = event.status.value
        alert.title = event.title
        await session.commit()

        try:
            deliveries = await deliver_message(session, message, registry)
            deliveries_sent += len(deliveries)
            for delivery in deliveries:
                await add_alert_delivery(
                    session,
                    alert_message_id=message.id,
                    channel=delivery["channel"],
                    status=delivery["status"],
                    external_id=delivery.get("external_id"),
                    error=delivery.get("error"),
                )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - channel failure never kills loop
            logger.exception("alert delivery failed for message %s: %s", message.id, exc)
        messages_sent += 1

    result = {
        "created": created,
        "messages_sent": messages_sent,
        "deliveries_sent": deliveries_sent,
    }
    if created or messages_sent:
        logger.info("alert engine cycle %s", result)
    return result


def _message_priority(alert: Alert, kind: AlertMessageKind, priority: AlertPriority) -> AlertPriority:
    if kind == AlertMessageKind.resolved:
        return AlertPriority.p4
    if kind == AlertMessageKind.severity:
        return priority
    if alert.message_count > 0 and kind == AlertMessageKind.update:
        return alert.priority
    return priority