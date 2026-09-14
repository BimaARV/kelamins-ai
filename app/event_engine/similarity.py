"""Deterministic text similarity used by the Event Engine (Phase 2).

No ML: token-based similarity (Jaccard + cosine with term frequency) over
titles, descriptions, location hints and published-time proximity. Clustering
must work without AI (graceful degradation, spec core principle #6); KELA AI
(Phase 3) refines understanding later.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime  # noqa: TC003

_WORD = re.compile(r"[a-z0-9]+")

STOPWORDS = {
    # Indonesian
    "yang", "di", "ke", "dari", "dan", "atau", "pada", "akan", "untuk",
    "dengan", "ini", "itu", "sebagai", "pasca", "setelah", "sebelum",
    "juga", "serta", "dalam", "oleh", "para", "suatu", "adalah", "telah",
    "saat", "warga", "sejumlah", "kepada", "bisa", "dapat", "sudah",
    "masih", "akan", "sampai", "hanya", "agar", "agar",
    # English
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
    "with", "after", "before", "from", "that", "this", "they", "will",
}


def tokenize(text: str | None) -> Counter:
    if not text:
        return Counter()
    tokens = _WORD.findall(text.lower())
    return Counter(token for token in tokens if len(token) > 1 and token not in STOPWORDS)


def _tf(counter: Counter, weight: float = 1.0) -> dict[str, float]:
    total = sum(counter.values()) or 1
    return {token: (count / total) * weight for token, count in counter.items()}


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[token] * b[token] for token in common)
    norm_a = sum(v * v for v in a.values()) ** 0.5
    norm_b = sum(v * v for v in b.values()) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def jaccard(a: Counter, b: Counter) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def _bag(title: str | None, description: str | None) -> dict[str, float]:
    combined: dict[str, float] = _tf(tokenize(title), weight=2.0)
    for token, value in _tf(tokenize(description), weight=1.0).items():
        combined[token] = combined.get(token, 0.0) + value
    return combined


def _time_factor(pa: datetime | None, pb: datetime | None) -> float:
    if pa is None or pb is None:
        return 0.0
    hours = abs((pa - pb).total_seconds()) / 3600.0
    if hours <= 24:
        return 1.0
    if hours >= 168:
        return 0.0
    return 1.0 - (hours - 24) / 144.0


def _location_factor(la: str | None, lb: str | None) -> float:
    if not la or not lb:
        return 0.5
    return 1.0 if la.strip().lower() == lb.strip().lower() else 0.0


def article_similarity(a: dict, b: dict) -> float:
    text_sim = cosine(_bag(a.get("title"), a.get("description")),
                      _bag(b.get("title"), b.get("description")))
    time_sim = _time_factor(a.get("published_at"), b.get("published_at"))
    loc_sim = _location_factor(a.get("location_name"), b.get("location_name"))
    return round(0.7 * text_sim + 0.15 * time_sim + 0.15 * loc_sim, 4)