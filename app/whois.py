"""Real IP registration lookup via RDAP — no AI guessing.

The chat AI used to hallucinate WHOIS answers (e.g. a Telkom IP for a random
office range). This module replaces that with deterministic RDAP lookups
against the regional registries (APNIC/ARIN/RIPE/LACNIC/AFRINIC), falling back
through the list until one answers.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

RDAP_SERVERS = [
    "https://rdap.apnic.net/ip/",
    "https://rdap.arin.net/registry/ip/",
    "https://rdap.db.ripe.net/ip/",
    "https://rdap.lacnic.net/rdap/ip/",
    "https://rdap.afrinic.net/rdap/ip/",
]

DOMAIN_BOOTSTRAP = "https://rdap.org/domain/"

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DOMAIN_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}"
)


def is_public_ip(value: str) -> bool:
    """True for a routable public IPv4 (rejects private/loopback/reserved)."""
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return (
        ip.version == 4
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_multicast
        and not ip.is_link_local
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def extract_ip(text: str) -> str | None:
    m = IP_RE.search(text or "")
    if not m:
        return None
    candidate = m.group(0)
    if any(int(part) > 255 for part in candidate.split(".")):
        return None
    return candidate


def extract_domain(text: str) -> str | None:
    """Return a plausible DNS domain (e.g. ``cnnindonesia.com``) or None."""
    m = DOMAIN_RE.search((text or "").lower())
    if not m:
        return None
    domain = m.group(0).rstrip(".").strip()
    if domain.startswith("www."):
        domain = domain[4:]
    labels = domain.split(".")
    if not labels or len(labels) < 2:
        return None
    if any(not label or len(label) > 63 for label in labels):
        return None
    if len(domain) > 253:
        return None
    return domain


def _fn_values(data: dict) -> list[str]:
    values: list[str] = []
    for entity in data.get("entities", []):
        vcard = entity.get("vcardArray") or []
        if len(vcard) < 2:
            continue
        for line in vcard[1]:
            if line and len(line) > 3 and line[0] == "fn":
                values.append(str(line[3]))
    return values


def _parse(data: dict, base_url: str) -> dict:
    start = data.get("startAddress")
    end = data.get("endAddress")
    spans = " - ".join(x for x in (start, end) if x) or data.get("handle")
    return {
        "network": data.get("name"),
        "range": spans,
        "country": data.get("country"),
        "orgs": _fn_values(data),
        "type": data.get("type"),
        "source": base_url.split("/")[2],
    }


async def rdap_lookup(ip: str, *, client: httpx.AsyncClient | None = None) -> dict | None:
    """Resolve a public IPv4 address through RDAP, returns parsed dict or None."""
    if not is_public_ip(ip):
        return {"error": f"{ip} bukan IP publik."}

    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    try:
        for base in RDAP_SERVERS:
            try:
                resp = await client.get(base + ip)
            except httpx.HTTPError as exc:  # noqa: BLE001 - try next registrar
                logger.debug("rdap %s failed for %s: %s", base, ip, exc)
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except ValueError:  # noqa: BLE001
                continue
            result = _parse(data, base)
            result["ip"] = ip
            result["lookup_url"] = f"{base}{ip}"
            return result
        return None
    finally:
        if own:
            await client.aclose()


def _registrar_of(data: dict) -> str | None:
    for entity in data.get("entities", []):
        roles = entity.get("roles") or []
        if "registrar" not in roles:
            continue
        vcard = entity.get("vcardArray") or []
        if len(vcard) < 2:
            continue
        for line in vcard[1]:
            if line and len(line) > 3 and line[0] == "fn":
                return str(line[3])
    return None


def _events_of(data: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for ev in data.get("events", []):
        label = ev.get("eventAction")
        if label:
            out[label] = str(ev.get("eventDate") or "")
    return out


async def rdap_domain_lookup(domain: str) -> dict | None:
    """Resolve registration data for a DNS domain via RDAP bootstrap."""
    domain = extract_domain(domain)
    if not domain:
        return {"error": f"{domain!r} bukan domain valid."}
    url = f"{DOMAIN_BOOTSTRAP}{quote(domain)}"
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None

    events = _events_of(data)
    return {
        "domain": data.get("ldhName") or domain,
        "registrar": _registrar_of(data),
        "orgs": _fn_values(data),
        "created": events.get("registration"),
        "updated": events.get("last changed"),
        "expires": events.get("expiration"),
        "status": list(data.get("status") or []),
        "nameservers": [ns.get("ldhName") for ns in data.get("nameservers", []) if ns.get("ldhName")],
        "source": "rdap.org",
        "lookup_url": url,
    }


def format_whois_domain(domain: str, info: dict) -> str:
    """HTML-safe printable WHOIS card for a domain (no LLM involved)."""
    from app.interfaces.formatter import bold, esc

    lines = [bold(f"WHOIS (domain) — {esc(info.get('domain') or domain)}")]
    if info.get("registrar"):
        lines.append(f"\n  {bold('Registrar')}: {esc(info['registrar'])}")
    if info.get("orgs"):
        lines.append(f"\n  {bold('Organisasi/Pemilik')}: {esc(' / '.join(info['orgs']))}")
    for label, key in [("Dibuat", "created"), ("Update", "updated"), ("Kadaluarsa", "expires")]:
        value = info.get(key)
        if value:
            lines.append(f"\n  {bold(label)}: {esc(value)}")
    if info.get("nameservers"):
        lines.append(f"\n  {bold('Nameserver')}: {esc(', '.join(info['nameservers']))}")
    if info.get("status"):
        lines.append(f"\n  {bold('Status')}: {esc(', '.join(info['status']))}")
    if info.get("lookup_url"):
        lines.append(f"\n  Sumber: RDAP {esc(info.get('source') or '')} (<a href=\"{esc(info['lookup_url'])}\">lihat</a>)")
    return "\n".join(lines)


def format_whois(ip: str, info: dict) -> str:
    """HTML-safe printable WHOIS card (used directly, never through the LLM)."""
    from app.interfaces.formatter import bold, esc, mono

    lines = [bold(f"WHOIS — {esc(ip)}")]
    orgs = info.get("orgs") or []
    if orgs:
        lines.append(f"\n  {bold('Organisasi/Pemilik')}: {esc(' / '.join(orgs))}")
    if info.get("network"):
        lines.append(f"\n  {bold('Network')}: {esc(info['network'])}")
    if info.get("range"):
        lines.append(f"\n  {bold('Range')}: {esc(info['range'])}")
    if info.get("country"):
        lines.append(f"\n  {bold('Country')}: {esc(info['country'])}")
    if info.get("type"):
        lines.append(f"\n  {bold('Tipe')}: {esc(info['type'])}")
    if info.get("lookup_url"):
        lines.append(f"\n  Sumber: RDAP {esc(info.get('source') or '')} "
                     f"(<a href=\"{esc(info['lookup_url'])}\">lihat</a>)")
    lines.append("\nPrediksi AI : NO. Ini data langsung dari RDAP, fakta."
                 if orgs else "")
    return "\n".join(lines)