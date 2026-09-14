"""Content hashing for article deduplication.

A content hash lets the system detect *duplicate articles* (the same story
syndicated/copied). It does NOT decide whether two DIFFERENT articles describe
the same real-world event - that is the Event Engine's job (Phase 2).
"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def canonical_text(text: str | None) -> str:
    """Normalize raw text: strip HTML, unescape, collapse whitespace, lowercase."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    plain = soup.get_text(separator=" ")
    plain = _TAG_RE.sub(" ", plain)
    plain = _WS_RE.sub(" ", plain).strip()
    return plain.lower()


def article_content_hash(title: str | None, description: str | None, content: str | None) -> str:
    """Stable SHA-256 over normalized title + description + excerpt content."""
    t = canonical_text(title)
    d = canonical_text(description)
    c = canonical_text(content)
    if not (t or d or c):
        return hashlib.sha256("".encode()).hexdigest()
    payload = "|".join([t, d, c[:2000]])
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()