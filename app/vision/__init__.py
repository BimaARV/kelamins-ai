"""Vision Engine — OCR, image analysis, and chart generation.

OCR uses tesseract-ocr via pytesseract.  Image analysis goes through the
existing Ollama AI gateway when a multimodal model is available; otherwise
falls back to OCR-only.
"""

from app.vision.ocr import ocr_image
from app.vision.analyzer import analyze_image

__all__ = ["ocr_image", "analyze_image"]
