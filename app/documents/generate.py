"""Generate downloadable files (pdf/docx/txt/md/xlsx/png) from plain text.

Entry point: ``render_document(content, fmt, *, title, output_dir) -> path``.

This does NOT call AI — the text comes pre-computed (e.g. from a chat reply).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from app.documents.renderer import render_report

logger = logging.getLogger(__name__)

_EXT = {
    "pdf": ".pdf",
    "docx": ".docx",
    "txt": ".txt",
    "md": ".md",
    "xlsx": ".xlsx",
    "png": ".png",
}

FORMATS = tuple(_EXT)


def _plain(text: str) -> str:
    """Strip small-markup noise so PDF/DOCX/XLSX/PNG stay readable."""
    out = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)
    out = re.sub(r"`([^`\n]+)`", r"\1", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", out)
    out = re.sub(r"(?m)^\s*#{1,6}\s+", "", out)
    out = re.sub(r"(?m)^\s*[-*+]\s+", "• ", out)
    return out


def safe_title(title: str, max_len: int = 60) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in " _-." else "_" for ch in title.strip()
    ).strip(" _.")
    cleaned = cleaned[:max_len]
    return cleaned or f"kela_{int(datetime.now(timezone.utc).timestamp())}"


def render_document(
    content: str,
    fmt: str,
    *,
    title: str = "KELA AI Export",
    output_dir: str = "storage/reports",
) -> str:
    """Render ``content`` into a file of the given format.

    Returns the generated file path. Raises ``ValueError`` for unknown formats.
    """
    fmt = fmt.lower()
    if fmt not in _EXT:
        raise ValueError(f"unsupported format: {fmt}")

    os.makedirs(output_dir, exist_ok=True)
    stamp = int(datetime.now(timezone.utc).timestamp())
    path = os.path.join(output_dir, f"{safe_title(title)}_{stamp}{_EXT[fmt]}")

    body = _plain(content)
    if fmt in ("pdf", "docx"):
        render_report([], title=title, executive_summary=body, output_format=fmt, output_path=path)
    elif fmt in ("txt", "md"):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    elif fmt == "xlsx":
        _write_xlsx(body, path)
    elif fmt == "png":
        _write_png_card(content, title, path)

    logger.info("generated %s -> %s", fmt, path)
    return path


def _write_xlsx(content: str, path: str) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "KELA AI"
    for line in content.splitlines():
        ws.append([line])
    wb.save(path)


def _write_png_card(content: str, title: str, path: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    margin = 60
    line_h = 36
    header_h = 96
    body = content.strip() or "Tidak ada konten."

    font = ImageFont.load_default()
    max_w = 1400 - 2 * margin

    def wrapped_lines(raw: str, maxwidth: int) -> list[str]:
        out: list[str] = []
        for para in raw.split("\n"):
            if not para.strip():
                continue
            cur = ""
            for word in para.split():
                probe = cur + (" " if cur else "") + word
                if font.getlength(probe) <= maxwidth or not cur:
                    cur = probe
                else:
                    out.append(cur)
                    cur = word
            if cur:
                out.append(cur)
        return out

    lines = wrapped_lines(body, max_w)[:36]
    if len(wrapped_lines(body, max_w)) > 36:
        lines.append("… (dipotong, konten penuh ada di jawaban chat)")
    width = 1400
    height = margin * 2 + header_h + len(lines) * line_h

    img = Image.new("RGB", (width, height), "#0f172a")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, 10], fill="#f59e0b")

    y = margin
    draw.text((margin, y), f"KELA AI — {title}", fill="#fbbf24", font=font)
    y += header_h
    for ln in lines:
        draw.text((margin, y), ln, fill="#e2e8f0", font=font)
        y += line_h

    draw.text((margin, height - 40), f"generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
              fill="#64748b", font=font)
    img.save(path)