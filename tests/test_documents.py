"""Document Engine tests: parser dispatch, PDF/DOCX/TXT extraction, report render."""

from pathlib import Path

import pytest

from app.documents.extractor import chunk_text, normalize_text, DocumentResult
from app.documents.parser import detect_document_type, parse_document
from app.documents.renderer import render_report
from app.documents.pdf import generate_pdf_report, read_pdf
from app.documents.docx import read_docx, write_docx_report


# ---------------------------------------------------------------------------
# Extractor helpers
# ---------------------------------------------------------------------------

def test_normalize_text_collapses_whitespace():
    raw = "Hello   world.\r\n\r\n\r\nSecond para."
    assert normalize_text(raw) == "Hello world.\n\nSecond para."


def test_chunk_text_small_document():
    text = "Short document."
    assert chunk_text(text, 100) == ["Short document."]
    assert len(chunk_text(text, 5)) > 1  # must split when over max_chars


def test_chunk_text_large_document():
    text = "\n\n".join(f"Para {i} " + "x" * 50 for i in range(10))
    chunks = chunk_text(text, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 105 for c in chunks)  # small spill from separator
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


def test_document_result_defaults():
    result = DocumentResult()
    assert result.text == ""
    assert result.metadata == {}
    assert result.sections == []


# ---------------------------------------------------------------------------
# Parser detection and routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("doc.pdf", "pdf"),
        ("report.docx", "docx"),
        ("notes.txt", "txt"),
        ("notes.md", "markdown"),
        ("notes.markdown", "markdown"),
        ("image.PDF", "pdf"),
        ("unknown.bin", "other"),
    ],
)
def test_detect_document_type(name, expected):
    assert detect_document_type(name) == expected


def test_parse_txt_document(tmp_path):
    path = tmp_path / "hello.txt"
    path.write_text("Hello KELA!\n\nThis is a *markdown-ish* note.", encoding="utf-8")
    result = parse_document(str(path), "txt")
    assert "Hello KELA!" in result.text
    assert result.metadata.get("format") == "txt"


def test_parse_txt_auto_detect(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# Heading\n\nBody text.", encoding="utf-8")
    result = parse_document(str(path))
    assert result.metadata.get("format") == "markdown"
    assert "Body text" in result.text


def test_parse_unsupported_returns_empty(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"\x00\x01\x02")
    result = parse_document(str(path))
    assert result.text == ""
    assert result.metadata.get("format") == "other"


# ---------------------------------------------------------------------------
# PDF reading + generation
# ---------------------------------------------------------------------------

def test_generate_pdf_and_read_back(tmp_path):
    out_path = str(tmp_path / "report.pdf")
    generate_pdf_report(
        {
            "title": "Test Report",
            "executive_summary": "Ringkasan eksekutif.",
            "events": [
                {
                    "event_type": "news",
                    "title": "Gempa guncang Maluku",
                    "confidence": "high",
                    "article_count": 3,
                }
            ],
        },
        out_path,
    )
    assert Path(out_path).exists()
    assert Path(out_path).stat().st_size > 1000

    result = read_pdf(out_path)
    assert result.metadata.get("pages") == 1
    assert "Ringkasan eksekutif" in result.text


# ---------------------------------------------------------------------------
# DOCX reading + generation
# ---------------------------------------------------------------------------

def test_write_docx_and_read_back(tmp_path):
    out_path = str(tmp_path / "report.docx")
    write_docx_report(
        {
            "title": "Docx Report",
            "executive_summary": "Ringkasan dokumen.",
            "events": [
                {
                    "event_type": "earthquake",
                    "title": "Gempa Magnitudo 5.0",
                    "confidence": "high",
                    "article_count": 1,
                }
            ],
        },
        out_path,
    )
    assert Path(out_path).exists()

    result = read_docx(out_path)
    assert result.metadata.get("paragraphs", 0) >= 2
    assert "Ringkasan dokumen" in result.text


# ---------------------------------------------------------------------------
# Report renderer
# ---------------------------------------------------------------------------

def test_render_pdf_report(tmp_path):
    out = render_report(
        [
            {
                "event_type": "news",
                "title": "Berita utama",
                "confidence": "medium",
                "article_count": 2,
                "independent_source_count": 2,
            }
        ],
        title="Laporan Harian",
        executive_summary="Ringkasan laporan.",
        output_format="pdf",
        output_path=str(tmp_path / "out"),
    )
    assert out.endswith(".pdf")
    assert Path(out).exists()


def test_render_docx_report(tmp_path):
    out = render_report(
        [],
        title="Laporan Kosong",
        output_format="docx",
        output_path=str(tmp_path / "out"),
    )
    assert out.endswith(".docx")
    assert Path(out).exists()


def test_render_pdf_appendix(tmp_path):
    out = render_report(
        [],
        title="Report",
        appendix={"source_count": 4, "confidence": "high"},
        output_path=str(tmp_path / "appendix.pdf"),
    )
    assert Path(out).exists()