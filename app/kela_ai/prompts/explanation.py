"""Event explanation task prompt: natural-language explanation with cited facts."""

from __future__ import annotations

from app.kela_ai.prompts.system import kela_system_prompt


def explanation_prompt(context: str, focus: str | None = None) -> tuple[str, str]:
    focus_line = f"\nFokus pertanyaan: {focus}" if focus else ""
    user = (
        "Jelaskan event ini seperti menjawab user yang bertanya"
        f"{focus_line}.\n\n"
        f"{context}\n\n"
        "Keluarkan JSON dengan kunci:"
        '\n  "explanation": string, 3-6 kalimat penjelasan natural berbahasa Indonesia dengan '
        "ISI: apa yang terjadi, kapan, di mana, dan kenapa penting."
        '\n  "facts": array of { "claim": string, "source": string }, klaim faktual yang kamu '
        "pakai beserta sumber kanalnya."
        '\n  "limitations": string, keterbatasan data atau hal yang belum bisa dipastikan.'
        "\nJangan menambahkan interpretasi yang tidak didukung konteks."
    )
    return kela_system_prompt(), user