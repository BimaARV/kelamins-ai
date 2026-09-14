"""Verification task prompt: cross-source consistency verdict (spec section 9)."""

from __future__ import annotations

from app.kela_ai.prompts.system import kela_system_prompt


def verification_prompt(context: str) -> tuple[str, str]:
    user = (
        "Verifikasi konsistensi antar sumber untuk event ini.\n\n"
        f"{context}\n\n"
        "Keluarkan JSON dengan kunci:"
        '\n  "verdict": string, pilih: consistent (sumber sepakat) | conflicting (ada '
        "perbedaan/tabrakan) | insufficient (data terlalu sedikit untuk menilai)."
        '\n  "consistent_points": array of string, poin yang disepakati semua sumber.'
        '\n  "conflicts": array of string, perbedaan antar sumber jika ada (kosongkan jika tidak).'
        '\n  "independent_sources": integer, jumlah sumber independen yang meliput ini.'
        '\n  "assessment": string, 1-2 kalimat penilaian.'
        "\nHanya nilai berdasarkan teks kanal yang tertera; sebut nama kanal dalam assessment."
    )
    return kela_system_prompt(), user