"""Report renderer — orchestrates PDF/DOCX generation from event data.

This does NOT call AI.  AI summaries are pre-computed and passed in as data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.documents.pdf import generate_pdf_report
from app.documents.docx import write_docx_report

logger = logging.getLogger(__name__)


def render_report(
    events: list[dict],
    *,
    title: str = "KELA AI Monitoring Report",
    executive_summary: str = "",
    output_format: str = "pdf",
    output_path: str = "storage/reports/report.pdf",
    appendix: dict | None = None,
) -> str:
    """Render a monitoring report to PDF or DOCX.

    Parameters
    ----------
    events : list[dict]
        Each dict should contain: event_type, title, confidence,
        article_count, independent_source_count, occurred_at, description.
    title : str
        Report title.
    executive_summary : str
        Pre-computed summary text (may come from KELA AI).
    output_format : str
        ``"pdf"`` or ``"docx"``.
    output_path : str
        Destination file path.
    appendix : dict | None
        Optional key/value pairs appended at the end.

    Returns
    -------
    str
        The path of the generated file.
    """
    report_data = {
        "title": title,
        "executive_summary": executive_summary,
        "events": events,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "appendix": appendix or {},
    }

    if output_format == "docx":
        if not output_path.endswith(".docx"):
            output_path = output_path.rsplit(".", 1)[0] + ".docx"
        return write_docx_report(report_data, output_path)

    if not output_path.endswith(".pdf"):
        output_path = output_path.rsplit(".", 1)[0] + ".pdf"
    return generate_pdf_report(report_data, output_path)
