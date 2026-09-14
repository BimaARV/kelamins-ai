"""Classification task prompt: category, severity, tags."""

from __future__ import annotations

from app.kela_ai.prompts.system import kela_system_prompt


def classification_prompt(context: str) -> tuple[str, str]:
    user = (
        "Klasifikasikan event ini untuk routing pemberitahuan.\n\n"
        f"{context}\n\n"
        "Keluarkan JSON dengan kunci:"
        '\n  "category": string, pilih salah satu: bencana | politik | ekonomi | hukum | '
        "olahraga | kesehatan | teknologi | keamanan | lingkungan | nasional | internasional | lainnya."
        '\n  "severity": string, pilih salah satu: critical | high | normal | informational.'
        '\n  "reason": string, 1 kalimat alasan klasifikasi berdasarkan fakta yang ada.'
        '\n  "tags": array of string, maksimal 5 tag pendek.'
        "\nKlasifikasi harus konsisten dengan fakta di konteks; jangan menebak di luar data."
    )
    return kela_system_prompt(), user