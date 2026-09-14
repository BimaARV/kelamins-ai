"""ASN / ISP / geo lookup for IP addresses.

Primary source: ipwho.is (free HTTPS, no key). Fallback: ip-api.com (HTTP).
Only used for network diagnostics (``/asn``, geo-traceroute).
"""

from __future__ import annotations

import httpx

from app.interfaces.formatter import bold, esc, mono

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

IPWHO_IS_URL = "https://ipwho.is/"
IPAPI_URL_TMPL = "http://ip-api.com/json/{ip}?fields=status,as,asname,isp,org,country,countryCode,regionName,city,timezone,query"


def _normalize_ipwho(item: dict) -> dict | None:
    conn = item.get("connection") or {}
    if not item.get("success"):
        return None
    asn_number = conn.get("asn_number")
    asn_name = conn.get("asn")
    if asn_number is None:
        if isinstance(asn_name, int):
            asn_number = asn_name
        else:
            head = str(asn_name or "").strip().split()[0] if str(asn_name or "").strip() else ""
            if head.startswith("AS") and head[2:].isdigit():
                asn_number = int(head[2:])
    if isinstance(asn_name, int):
        asn_name = f"AS{asn_name}"
    tz = item.get("timezone")
    if isinstance(tz, dict):
        tz = tz.get("id")
    return {
        "asn": asn_number,
        "asn_name": asn_name,
        "isp": conn.get("isp"),
        "org": conn.get("org"),
        "country": item.get("country"),
        "country_code": item.get("country_code"),
        "region": item.get("region"),
        "city": item.get("city"),
        "tz": tz,
    }


def _normalize_ipapi(item: dict) -> dict | None:
    if item.get("status") != "success":
        return None
    asn_line = item.get("as") or ""
    asn_number = None
    asn_name = asn_line
    if " " in asn_line:
        head, _, rest = asn_line.partition(" ")
        if head.startswith("AS") and head[2:].isdigit():
            asn_number = int(head[2:])
            asn_name = rest or None
    return {
        "asn": asn_number,
        "asn_name": asn_name,
        "isp": item.get("isp"),
        "org": item.get("org"),
        "country": item.get("country"),
        "country_code": item.get("countryCode"),
        "region": item.get("regionName"),
        "city": item.get("city"),
        "tz": item.get("timezone"),
    }


async def asn_lookup(ip: str) -> dict | None:
    """Lookup a single IP → normalized dict, or None on failure."""
    try:
        async with httpx.AsyncClient(
            timeout=10.0, headers={"User-Agent": BROWSER_UA}
        ) as client:
            resp = await client.get(f"{IPWHO_IS_URL}{ip}")
            if resp.status_code == 200:
                info = _normalize_ipwho(resp.json())
                if info:
                    return info
    except (httpx.HTTPError, ValueError):
        pass
    try:
        async with httpx.AsyncClient(
            timeout=8.0, headers={"User-Agent": BROWSER_UA}, follow_redirects=True
        ) as client:
            resp = await client.get(IPAPI_URL_TMPL.format(ip=ip))
            if resp.status_code == 200:
                info = _normalize_ipapi(resp.json())
                if info:
                    return info
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def bulk_asn_lookup(ips: list[str]) -> dict[str, dict]:
    """Batch resolve many IPs (ipwho.is bulk) → ``{ip: info}``."""
    result: dict[str, dict] = {}
    if not ips:
        return result
    unique = list(dict.fromkeys(ips))
    try:
        async with httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": BROWSER_UA}
        ) as client:
            resp = await client.post(IPWHO_IS_URL, json=unique)
            if resp.status_code == 200:
                items = resp.json()
                if isinstance(items, list):
                    for ip, item in zip(unique, items, strict=False):
                        if isinstance(item, dict):
                            info = _normalize_ipwho(item)
                            if info:
                                result[ip] = info
    except (httpx.HTTPError, ValueError):
        pass
    missing = [ip for ip in unique if ip not in result]
    for ip in missing:
        info = await asn_lookup(ip)
        if info:
            result[ip] = info
    return result


def format_asn(ip: str, info: dict) -> str:
    lines = [bold(f"ASN — {mono(esc(ip))}")]
    negara = info.get("country") or "?"
    if info.get("country_code"):
        negara = f"{negara} ({info.get('country_code')})"
    asn_s = f"AS{info.get('asn')}" if info.get("asn") else "?"
    rows = [
        ("ASN", asn_s),
        ("Org/ISP", info.get("org") or info.get("isp") or "?"),
        ("Negara", negara),
        ("Region", info.get("region") or "?"),
        ("Kota", info.get("city") or "?"),
        ("Zona", info.get("tz") or "?"),
    ]
    for label, value in rows:
        lines.append(f"  {label}: {mono(esc(str(value)))}")
    lines.append("\nsumber: ipwho.is")
    return "\n".join(lines)