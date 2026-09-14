"""Generic document parser — detects type and routes to the right reader."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.documents.extractor import DocumentResult

logger = logging.getLogger(__name__)

_EXT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".txt": "txt",
    ".md": "markdown",
    ".markdown": "markdown",
}


def detect_document_type(file_path: str) -> str:
    """Return a DocumentType string based on file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_MAP.get(ext, "other")


def parse_document(file_path: str, doc_type: str | None = None) -> DocumentResult:
    """Parse *file_path* and return a unified DocumentResult.

    If *doc_type* is not provided it is auto-detected from the extension.
    """
    if doc_type is None:
        doc_type = detect_document_type(file_path)

    if doc_type == "pdf":
        from app.documents.pdf import read_pdf

        return read_pdf(file_path)

    if doc_type == "docx":
        from app.documents.docx import read_docx

        return read_docx(file_path)

    if doc_type in ("txt", "markdown"):
        from app.documents.extractor import extract_file_metadata, normalize_text

        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        text = normalize_text(raw)
        meta = extract_file_metadata(file_path)
        meta["file_path"] = file_path
        meta["format"] = doc_type
        return DocumentResult(text=text, metadata=meta)

    # Unsupported — return empty
    logger.warning("unsupported document type=%s path=%s", doc_type, file_path)
    return DocumentResult(metadata={"file_path": file_path, "format": doc_type})
