"""PDF reading (pypdf) and generation (reportlab)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.documents.extractor import DocumentResult, normalize_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PDF reading
# ---------------------------------------------------------------------------

def read_pdf(file_path: str) -> DocumentResult:
    """Extract text and metadata from a PDF file using pypdf."""
    from pypdf import PdfReader

    reader = PdfReader(file_path)
    meta = reader.metadata or {}

    metadata: dict = {
        "title": getattr(meta, "title", None) or "",
        "author": getattr(meta, "author", None) or "",
        "pages": len(reader.pages),
        "file_path": file_path,
    }

    pages_text: list[str] = []
    for page in reader.pages:
        extracted = page.extract_text()
        if extracted:
            pages_text.append(extracted)

    text = normalize_text("\n\n".join(pages_text))

    sections: list[dict] = []
    for idx, page_text in enumerate(pages_text, start=1):
        snippet = page_text[:200].strip()
        if snippet:
            sections.append({"page": idx, "preview": snippet})

    return DocumentResult(text=text, metadata=metadata, sections=sections)


# ---------------------------------------------------------------------------
# PDF report generation (reportlab)
# ---------------------------------------------------------------------------

def generate_pdf_report(report_data: dict, output_path: str) -> str:
    """Render a monitoring report as PDF using reportlab.

    *report_data* keys:
        title, executive_summary, events (list[dict]),
        generated_at, appendix (optional dict)
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    doc = SimpleDocTemplate(output_path, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm)
    styles = getSampleStyleSheet()
    story: list = []

    title_style = ParagraphStyle("ReportTitle", parent=styles["Title"], fontSize=18, spaceAfter=12)
    heading_style = ParagraphStyle("ReportHeading", parent=styles["Heading2"], fontSize=13, spaceAfter=8)
    body_style = ParagraphStyle("ReportBody", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=6)
    small_style = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8, textColor="grey")

    story.append(Paragraph(report_data.get("title", "KELA AI Report"), title_style))
    story.append(Spacer(1, 0.5 * cm))

    gen_at = report_data.get("generated_at", "")
    story.append(Paragraph(f"Generated: {gen_at}", small_style))
    story.append(Spacer(1, 0.5 * cm))

    # Executive summary
    summary = report_data.get("executive_summary", "")
    if summary:
        story.append(Paragraph("Executive Summary", heading_style))
        story.append(Paragraph(summary, body_style))
        story.append(Spacer(1, 0.4 * cm))

    # Events table
    events = report_data.get("events", [])
    if events:
        story.append(Paragraph("Events", heading_style))
        header = ["#", "Type", "Title", "Confidence", "Sources"]
        rows = [header]
        for idx, ev in enumerate(events, 1):
            rows.append([
                str(idx),
                ev.get("event_type", ""),
                ev.get("title", "")[:60],
                ev.get("confidence", ""),
                str(ev.get("article_count", "")),
            ])

        table = Table(rows, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), "#333333"),
            ("TEXTCOLOR", (0, 0), (-1, 0), "white"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, "#cccccc"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), ["#f9f9f9", "white"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.4 * cm))

    # Appendix
    appendix = report_data.get("appendix")
    if appendix:
        story.append(Paragraph("Appendix", heading_style))
        for key, value in appendix.items():
            story.append(Paragraph(f"<b>{key}</b>: {value}", body_style))

    doc.build(story)
    return output_path
