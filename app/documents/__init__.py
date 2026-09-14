"""Document Engine — file parsing, text extraction, and report rendering.

AI decides WHAT to do with a document.  This engine does the actual file work
(read, parse, generate) and can operate independently of the AI layer.
"""

from app.documents.parser import parse_document
from app.documents.renderer import render_report

__all__ = ["parse_document", "render_report"]
