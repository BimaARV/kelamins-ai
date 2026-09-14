"""OCR via tesseract-ocr (pytesseract wrapper)."""

from __future__ import annotations

import logging
import shutil

logger = logging.getLogger(__name__)


def _tesseract_available(cmd: str = "/usr/bin/tesseract") -> bool:
    return shutil.which(cmd) is not None


def ocr_image(
    image_path: str,
    languages: str = "ind+eng",
    tesseract_cmd: str = "/usr/bin/tesseract",
) -> str:
    """Run OCR on *image_path* and return the extracted text.

    Parameters
    ----------
    image_path : str
        Path to a PNG/JPEG/TIFF image.
    languages : str
        Tesseract language codes (default ``ind+eng``).
    tesseract_cmd : str
        Path to the tesseract binary.

    Returns
    -------
    str
        Extracted text, or empty string on failure.
    """
    if not _tesseract_available(tesseract_cmd):
        logger.warning("tesseract binary not found at %s — OCR unavailable", tesseract_cmd)
        return ""

    try:
        import pytesseract
        from PIL import Image

        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang=languages)
        return text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR failed for %s: %s", image_path, exc)
        return ""
