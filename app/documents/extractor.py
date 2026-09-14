"""Shared text normalisation and metadata extraction utilities."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass
class DocumentResult:
    """Unified result from any document parser."""

    text: str = ""
    metadata: dict = field(default_factory=dict)
    sections: list[dict] = field(default_factory=list)


def normalize_text(text: str) -> str:
    """Collapse excessive whitespace while preserving paragraph breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_file_metadata(file_path: str) -> dict:
    """Return basic OS-level metadata for a file."""
    stat = os.stat(file_path)
    return {
        "file_size": stat.st_size,
        "modified_at": stat.st_mtime,
    }


def chunk_text(text: str, max_chars: int = 4000) -> list[str]:
    """Split *text* into chunks of at most *max_chars* characters.

    Attempts to break at paragraph or sentence boundaries.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = (current + "\n\n" + paragraph).strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= max_chars:
            current = paragraph
        else:
            for i in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[i : i + max_chars])
    if current:
        chunks.append(current)
    return chunks
