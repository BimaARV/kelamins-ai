"""Prompt builders for KELA AI (spec sections 2, 11, 13).

Prompts enforce KELA's core rules:
- source-first: every factual claim names its source
- facts from monitoring data; AI never invents or originates data
- interpretation is clearly separated from facts
- concise, natural Indonesian - no stiff bot voice
Output contract for each task is strict JSON so results are stable and parseable.
"""

from __future__ import annotations

from app.kela_ai.prompts.classification import classification_prompt
from app.kela_ai.prompts.explanation import explanation_prompt
from app.kela_ai.prompts.summary import summary_prompt
from app.kela_ai.prompts.system import kela_system_prompt
from app.kela_ai.prompts.verification import verification_prompt

__all__ = [
    "kela_system_prompt",
    "summary_prompt",
    "classification_prompt",
    "verification_prompt",
    "explanation_prompt",
    "build_event_context",
]


def build_event_context(payload: dict, max_articles: int = 5) -> str:
    """Compact, deterministic context block derived only from stored facts."""
    event = payload.get("event") or {}
    articles = payload.get("articles") or []

    lines = [
        f"EVENT_ID: {event.get('id')}",
        f"EVENT_TYPE: {event.get('event_type')}",
        f"STATUS: {event.get('status')}",
        f"OCCURRED_AT: {event.get('occurred_at')}",
        f"LOCATION: {event.get('location_name') or '-'}",
        f"CONFIDENCE: {event.get('confidence') or '-'}",
        f"TITLE: {event.get('title')}",
        f"DESCRIPTION: {event.get('description') or '-'}",
        "",
        "ARTIKEL (DARI MONITORING):",
    ]
    for article in articles[:max_articles]:
        lines.append(
            f"- [{article.get('relation_type')} | {article.get('source')} | "
            f"{article.get('published_at') or '-'}] {article.get('title')}"
            f"{(' — ' + article['description'][:400]) if article.get('description') else ''}"
            f" ({article.get('url')})"
        )
    if len(articles) > max_articles:
        lines.append(f"... dan {len(articles) - max_articles} artikel lain sudah monitoring.")
    return "\n".join(lines)