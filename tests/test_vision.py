"""Vision Engine tests: OCR wrapper + image analysis + chart generation."""

from pathlib import Path

import pytest

from app.vision.analyzer import image_mime_type, image_to_base64
from app.vision.ocr import ocr_image
from app.vision.image_generation import generate_chart


# ---------------------------------------------------------------------------
# Base64 / MIME helpers
# ---------------------------------------------------------------------------

def _make_png(tmp_path) -> str:
    """Generate a tiny valid PNG using Pillow."""
    from PIL import Image

    image = Image.new("RGB", (10, 10), color=(255, 255, 255))
    path = tmp_path / "img.png"
    image.save(str(path))
    return str(path)


def test_image_to_base64(tmp_path):
    path = _make_png(tmp_path)
    data = image_to_base64(path)
    assert data
    assert len(data) > 10


def test_image_mime_type(tmp_path):
    path = _make_png(tmp_path)
    assert image_mime_type(path) == "image/png"


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def test_ocr_image_missing_binary_returns_empty(tmp_path):
    path = _make_png(tmp_path)
    text = ocr_image(path, tesseract_cmd="/nonexistent/tesseract")
    assert text == ""


def test_ocr_image_nonexistent_path():
    text = ocr_image("/nonexistent/file.png", tesseract_cmd="/usr/bin/tesseract")
    assert text == ""


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def test_generate_bar_chart(tmp_path):
    out = generate_chart(
        "bar",
        [{"label": "A", "value": 10}, {"label": "B", "value": 20}],
        str(tmp_path / "chart.png"),
        title="Test Chart",
    )
    assert out
    assert Path(out).exists()
    assert Path(out).stat().st_size > 0


def test_generate_pie_chart(tmp_path):
    out = generate_chart(
        "pie",
        [{"label": "X", "value": 3}, {"label": "Y", "value": 7}],
        str(tmp_path / "pie.png"),
    )
    assert out
    assert Path(out).exists()


def test_generate_unsupported_chart(tmp_path):
    out = generate_chart(
        "radar",
        [{"label": "A", "value": 1}],
        str(tmp_path / "bad.png"),
    )
    assert out == ""
    assert not Path(tmp_path / "bad.png").exists()