"""Summary task prompt: compact, source-referenced event summary."""

from __future__ import annotations

from app.kela_ai.prompts.system import kela_system_prompt


def summary_prompt(context: str) -> tuple[str, str]:
    user = (
        "Rangkum event ini untuk briefing singkat.\n\n"
        f"{context}\n\n"
        "Keluarkan JSON dengan kunci:"
        '\n  "summary": string, 2-4 kalimat ringkas berbasis fakta dan menyebut sumber.'
        '\n  "key_points": array of string, 3-5 poin penting disertai sumber dalam tanda kurung.'
        '\n  "sumber_utama": array of string, nama kanal/feed yang membahas event ini.'
        "\nJangan tambahkan klaim di luar data yang diberikan."
    )
    return kela_system_prompt(), user