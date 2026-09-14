"""URL validation & canonicalization for collected links.

Never trust a URL straight from a feed. Always validate scheme/host before
storing. The AI layer must never invent URLs - only real collected links pass.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_VALID_SCHEMES = {"http", "https"}


def is_valid_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in _VALID_SCHEMES:
        return False
    if not parsed.netloc:
        return False
    if " " in parsed.netloc:
        return False
    return True


def canonical_url(url: str | None) -> str | None:
    """Lowercase scheme/netloc, drop fragment, keep sorted query keys."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in _VALID_SCHEMES or not parsed.netloc:
        return None
    query = urlencode(sorted(parse_qsl(parsed.query)))
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, query, "")
    )