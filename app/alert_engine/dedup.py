"""Alert deduplication (core principle 8, spec section 14).

Never alert per article. Each event has exactly one alert thread; new messages
are added only when the event genuinely changed since the last notified state.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Alert, AlertMessageKind, AlertPriority, Event


@dataclass(frozen=True)
class AlertAction:
    kind: AlertMessageKind
    signature: str


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _signature(priority: AlertPriority, event: Event, article_count: int, independent: int) -> str:
    return f"{priority.value}|{_status_value(event.status)}|{article_count}|{independent}"


def _parse_counts(signature: str | None) -> tuple[int, int]:
    if not signature:
        return 0, 0
    parts = signature.split("|")
    try:
        articles = int(parts[2])
    except (IndexError, ValueError):
        articles = 0
    try:
        independent = int(parts[3])
    except (IndexError, ValueError):
        independent = 0
    return articles, independent


async def plan_alert(
    session: AsyncSession,
    event: Event,
    alert: Alert | None,
    stats: dict,
    priority: AlertPriority,
    reason: str,
) -> AlertAction | None:
    del session, reason  # decision is pure compared against stored alert state
    article_count = stats.get("article_count", 0)
    independent = stats.get("independent_source_count", 0)
    signature = _signature(priority, event, article_count, independent)

    if alert is None:
        if _status_value(event.status) in ("resolved", "closed"):
            return None
        return AlertAction(kind=AlertMessageKind.initial, signature=signature)

    if alert.last_signature == signature:
        return None

    event_status = _status_value(event.status)
    if event_status in ("resolved", "closed"):
        if alert.status == event_status:
            return None
        return AlertAction(kind=AlertMessageKind.resolved, signature=signature)

    if priority != alert.priority:
        return AlertAction(kind=AlertMessageKind.severity, signature=signature)

    last_articles, last_independent = _parse_counts(alert.last_signature)
    new_articles = article_count - last_articles
    new_sources = independent - last_independent
    if (
        new_sources >= settings.alert_update_min_new_sources
        or new_articles >= settings.alert_update_min_new_articles
    ):
        if alert.last_sent_at is not None:
            from datetime import datetime, timezone

            elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - alert.last_sent_at).total_seconds()
            if elapsed < settings.alert_update_cooldown_seconds:
                return None
        return AlertAction(kind=AlertMessageKind.update, signature=signature)

    return None