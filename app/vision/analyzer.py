"""Image analysis via Ollama multimodal models.

Sends the image to the existing KELA AI gateway (ollama_client).  If the
configured model does not support vision input the module falls back to
OCR-only with a warning.
"""

from __future__ import annotations

import base64
import logging
import mimetypes

logger = logging.getLogger(__name__)


def image_to_base64(image_path: str) -> str:
    """Read an image file and return its base64-encoded content."""
    with open(image_path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("utf-8")


def image_mime_type(image_path: str) -> str:
    """Return the MIME type for *image_path* (defaults to ``image/png``)."""
    mime, _ = mimetypes.guess_type(image_path)
    return mime or "image/png"


async def analyze_image(
    image_path: str,
    prompt: str = "Describe this image in detail. If it contains text, extract and list all text.",
    *,
    ocr_fallback: bool = True,
    tesseract_cmd: str = "/usr/bin/tesseract",
    ocr_languages: str = "ind+eng",
) -> dict:
    """Analyse an image using the KELA AI gateway.

    Returns
    -------
    dict
        ``{"method": "vision"|"ocr", "text": str, "error": str|None}``
    """
    result: dict = {"method": "vision", "text": "", "error": None}

    # Try Ollama vision first
    try:
        from app.kela_ai.gateway import build_gateway
        from app.config import settings

        gateway = build_gateway(settings)
        if gateway.client:
            b64 = image_to_base64(image_path)
            mime = image_mime_type(image_path)
            response = await gateway.client.chat.completions.create(
                model=gateway.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            },
                        ],
                    }
                ],
                max_tokens=settings.ai_max_tokens,
            )
            text = response.choices[0].message.content or ""
            result["text"] = text.strip()
            return result
    except Exception as exc:  # noqa: BLE001
        logger.info("vision analysis unavailable (%s) — falling back to OCR", exc)

    # Fallback to OCR
    result["method"] = "ocr"
    if ocr_fallback:
        try:
            from app.vision.ocr import ocr_image

            result["text"] = ocr_image(image_path, languages=ocr_languages, tesseract_cmd=tesseract_cmd)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
    return result
