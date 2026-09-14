"""BMKG weather forecast collector.

FACT RULE (mirrors spec section 9 for earthquakes): all weather facts
(temperature, humidity, wind, precipitation, visibility) MUST come from the
authoritative BMKG public API (api.bmkg.go.id). The AI may only interpret and
describe them - it never originates weather data.

Endpoint (official, documented at github.com/infoBMKG/data-cuaca):
    GET https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4=<village code>
requires a full browser User-Agent (the API rejects default clients).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; KELA-AI/0.1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def parse_weather_payload(payload: dict | None) -> tuple[dict, list[dict]]:
    """Normalize a BMKG weather response into (location_info, forecast_rows)."""
    if not payload:
        return {}, []
    lokasi = payload.get("lokasi") or {}
    data_nodes = payload.get("data") or []
    day_lists: list[list[dict]] = []
    for node in data_nodes:
        cuaca = node.get("cuaca")
        if isinstance(cuaca, list):
            day_lists.extend([d for d in cuaca if isinstance(d, list)])

    info = {
        "adm1": str(lokasi.get("adm1") or "") or None,
        "adm2": str(lokasi.get("adm2") or "") or None,
        "adm3": str(lokasi.get("adm3") or "") or None,
        "adm4": str(lokasi.get("adm4") or "").strip() or None,
        "provinsi": lokasi.get("provinsi"),
        "kotkab": lokasi.get("kotkab"),
        "kecamatan": lokasi.get("kecamatan"),
        "desa": lokasi.get("desa"),
        "latitude": lokasi.get("lat"),
        "longitude": lokasi.get("lon"),
        "timezone": lokasi.get("timezone"),
    }

    rows: list[dict] = []
    for day in day_lists:
        for entry in day:
            if not isinstance(entry, dict):
                continue
            rows.append(
                {
                    "forecast_datetime": _parse_utc(entry.get("datetime")),
                    "analysis_date": _parse_utc(entry.get("analysis_date")),
                    "weather_code": entry.get("weather"),
                    "weather_description": entry.get("weather_desc"),
                    "weather_description_en": entry.get("weather_desc_en"),
                    "temperature_c": _as_float(entry.get("t")),
                    "humidity_pct": _as_float(entry.get("hu")),
                    "precipitation_mm": _as_float(entry.get("tp")),
                    "wind_deg": entry.get("wd_deg"),
                    "wind_dir": entry.get("wd"),
                    "wind_to_dir": entry.get("wd_to"),
                    "wind_speed_kmh": _as_float(entry.get("ws")),
                    "visibility_text": entry.get("vs_text"),
                    "raw_data": entry,
                }
            )
    return info, rows


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_forecast(adm4: str, timeout: float = 20.0, endpoint: str | None = None) -> dict:
    """Fetch the raw BMKG forecast payload for one adm4 village code."""
    url = endpoint or "https://api.bmkg.go.id/publik/prakiraan-cuaca"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as client:
        resp = await client.get(url, params={"adm4": adm4})
        resp.raise_for_status()
        return resp.json()