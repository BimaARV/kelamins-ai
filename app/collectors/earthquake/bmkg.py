"""BMKG earthquake collector.

FACT RULE (spec section 9): all earthquake facts (magnitude, depth, lat/lon,
origin time, place) MUST come from the authoritative BMKG feed. The AI layer
may only summarize/explain/word alerts - it never originates quake facts.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; KELA-AI/0.1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_FLOAT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _parse_magnitude(mag: Any) -> float | None:
    if mag is None:
        return None
    m = _FLOAT_RE.search(str(mag))
    return float(m.group()) if m else None


def _parse_depth(depth: Any) -> float | None:
    if depth is None:
        return None
    m = _FLOAT_RE.search(str(depth))
    return float(m.group()) if m else None


def _parse_coordinate(value: str, coordinate: str) -> float | None:
    """Parse BMKG 'Lintang'/'Bujur' style, e.g. '8.57 LS' -> -8.57.

    Suffixes: LU (north +), LS (south -), BT (east +), BB (west -).
    """
    if not value:
        return None
    m = _FLOAT_RE.search(value)
    if not m:
        return None
    num = float(m.group())
    upper = value.upper()
    if coordinate == "lat":
        if "LS" in upper:
            num = -num
        elif "LU" in upper:
            num = num
        else:
            return None
    else:
        if "BB" in upper:
            num = -num
        elif "BT" in upper:
            num = num
        else:
            return None
    return num


def _external_id(gempa: dict) -> str:
    """Prefer a provider event id when present; otherwise derive a stable id
    from authoritative fields so repeated polls of the same quake collide."""
    if gempa.get("eventid"):
        return str(gempa["eventid"])
    anchor = "|".join(
        str(gempa.get(k)) for k in ("DateTime", "Coordinates", "Magnitude", "Kedalaman", "Wilayah")
    )
    return "latest_" + hashlib.sha256(anchor.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _parse_occurred_at(gempa: dict) -> datetime | None:
    for key in ("DateTime", "datetime"):
        raw = gempa.get(key)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def parse_bmkg_gempa(gempa: dict | None) -> dict | None:
    """Normalize one BMKG 'gempa' object into an earthquake fact dict.

    Returns None when the payload is empty or unusable (treated as no data,
    which is preferable to fabricating a quake).
    """
    if not gempa:
        return None

    coordinates = str(gempa.get("Coordinates") or "").strip()
    lat = lon = None
    m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)", coordinates)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
    if lat is None:
        lat = _parse_coordinate(str(gempa.get("Lintang") or ""), "lat")
    if lon is None:
        lon = _parse_coordinate(str(gempa.get("Bujur") or ""), "lon")

    return {
        "external_id": _external_id(gempa),
        "magnitude": _parse_magnitude(gempa.get("Magnitude")),
        "depth_km": _parse_depth(gempa.get("Kedalaman")),
        "latitude": lat,
        "longitude": lon,
        "place": str(gempa.get("Wilayah") or "").strip() or None,
        "occurred_at": _parse_occurred_at(gempa),
        "source": "bmkg",
        "raw_data": gempa,
    }


async def fetch_latest_earthquake(feed_url: str, timeout: float = 20.0) -> dict | None:
    """Fetch the latest BMKG event from the authoritative feed."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = await client.get(feed_url)
        resp.raise_for_status()
        data = resp.json()
    return parse_bmkg_gempa(data.get("Infogempa", {}).get("gempa"))