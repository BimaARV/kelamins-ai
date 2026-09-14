"""Prompt system tests: KELA persona rules and deterministic context rendering."""

from app.kela_ai.prompts import (
    build_event_context,
    classification_prompt,
    explanation_prompt,
    kela_system_prompt,
    summary_prompt,
    verification_prompt,
)

PAYLOAD = {
    "event": {
        "id": 7,
        "event_type": "earthquake",
        "status": "active",
        "occurred_at": "2026-09-10 04:00:00",
        "location_name": "Maluku Barat Daya",
        "confidence": "high",
        "title": "Gempa M 5.2 guncang Maluku Barat Daya",
        "description": "BMKG melaporkan gempa magnitudo 5.2.",
    },
    "articles": [
        {
            "relation_type": "primary",
            "source": "BMKG",
            "published_at": "2026-09-10 04:02:00",
            "title": "Gempa M 5.2 guncang Maluku Barat Daya",
            "description": "Tidak berpotensi tsunami.",
            "url": "https://example.com/a",
        },
        {
            "relation_type": "related",
            "source": "Antara",
            "published_at": "2026-09-10 04:10:00",
            "title": "Gempa Maluku Barat Daya tidak berpotensi tsunami",
            "description": "Badan meteorologi memastikan tidak ada potensi tsunami.",
            "url": "https://example.com/b",
        },
    ],
}


def test_system_prompt_enforces_source_first_and_no_fabrication():
    system = kela_system_prompt()
    assert "Bahasa Indonesia" in system
    assert "JANGAN pernah" in system
    assert "sumber" in system.lower()


def test_system_prompt_has_gaul_persona():
    system = kela_system_prompt()
    assert "gaul" in system
    assert "no cap" in system
    assert "kasar" in system


def test_system_prompt_knows_kelamins_identity_and_expertise():
    system = kela_system_prompt()
    assert "The KELAMINS Project" in system
    assert "Knowledge-driven Engineering for Layered Architecture" in system
    assert "Data Center" in system
    assert "NAP" in system
    assert "ISP" in system
    assert "coding" in system


def test_system_prompt_avoids_crude_words():
    import re

    system = kela_system_prompt()
    for word in ("kontol", "bangsat", "memek", "goblok", "tai"):
        assert not re.search(rf"\b{word}\b", system, flags=re.I)
    assert "70% Indonesia" in system


def test_system_prompt_bans_latex_notation():
    system = kela_system_prompt()
    assert "\\rightarrow" in system
    assert "\\frac" in system
    assert "LaTeX" in system
    assert "JANGAN" in system


def test_system_prompt_encourages_readable_formatting():
    system = kela_system_prompt()
    assert "**teks tebal**" in system
    assert "bullet list" in system
    assert "'→'" in system


def test_system_prompt_injects_asia_jakarta_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    system = kela_system_prompt()
    now = datetime.now(ZoneInfo("Asia/Jakarta"))
    assert "Asia/Jakarta" in system
    assert now.strftime("%Y") in system
    assert now.strftime("%d %B %Y") in system


def test_context_is_deterministic_and_contains_facts():
    also_known = PAW = build_event_context(PAYLOAD)
    assert PAW == build_event_context(PAYLOAD)
    assert "EVENT_ID: 7" in also_known
    assert "Maluku Barat Daya" in also_known
    assert "BMKG" in also_known


def test_context_lists_articles_with_sources():
    context = build_event_context(PAYLOAD)
    assert "- [primary | BMKG |" in context
    assert "- [related | Antara |" in context


def test_prompt_builders_return_system_and_user_messages():
    for builder in (summary_prompt, classification_prompt, verification_prompt):
        system, user = builder(build_event_context(PAYLOAD))
        assert "KELA" in system
        assert "JSON" in user

    system, user = explanation_prompt(build_event_context(PAYLOAD), focus="ada tsunami?")
    assert "JSON" in user
    assert "tsunami" in user


def test_prompt_types_are_covered_by_wanted_tasks():
    from app.kela_ai.services import _TASKS, wanted_tasks

    for task in wanted_tasks("news") + wanted_tasks("earthquake"):
        assert task in _TASKS