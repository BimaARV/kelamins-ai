"""Geo-traceroute — run ``traceroute`` and annotate every hop with ASN/ISP/country."""

from __future__ import annotations

import re

from app.interfaces.formatter import bold, esc, mono, num
from app.netinfo import bulk_asn_lookup
from app.shell import run_network_cmd

_HOP_IP = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


def _valid_ip(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


def parse_traceroute_hops(text: str) -> list[str]:
    """Extract hop IPs from traceroute output (bare IP or 'host (IP)')."""
    hops: list[str] = []
    for line in (text or "").splitlines():
        if not re.match(r"^\s*\d+\s", line):
            continue
        m = _HOP_IP.search(line)
        if not m:
            continue
        ip = m.group(1)
        if _valid_ip(ip):
            hops.append(ip)
    return hops


async def geotrace(target: str) -> dict:
    """Traceroute + ASN resolution for each hop."""
    result = await run_network_cmd(["traceroute", target], timeout=45.0)
    hops = parse_traceroute_hops(result.get("stdout") or "")
    info = await bulk_asn_lookup(hops)
    return {"target": target, "rc": result["rc"], "hops": hops, "info": info}


def format_geotrace(geo: dict) -> str:
    target = geo.get("target") or "?"
    lines = [bold(f"Geo-Traceroute → {mono(esc(target))}")]
    info = geo.get("info") or {}
    hops = geo.get("hops") or []
    if geo.get("rc"):
        lines.append(f"\n  exit code: {geo['rc']}")
    if not hops:
        lines.append("\n  (tidak ada hop yang ter-resolve)")
        return "\n".join(lines)
    for i, ip in enumerate(hops, start=1):
        row = info.get(ip)
        if row:
            asn = f"AS{row.get('asn')}" if row.get("asn") else "AS?"
            org = row.get("org") or row.get("isp") or "?"
            country = row.get("country") or "?"
            city = (row.get("city") or "") + (", " if row.get("city") else "")
            lines.append(
                f"  {i}. {mono(esc(ip))} — {esc(org)} · {mono(esc(asn))} · {esc(city)}{esc(country)}"
            )
        else:
            lines.append(f"  {i}. {mono(esc(ip))} — ?")
    lines.append("\nsumber asn: ipwho.is")
    return "\n".join(lines)