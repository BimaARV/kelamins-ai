"""Free-text network-diag interception — real tools, no AI guessing.

Catches ``traceroute`` / ``ping`` / ``asn`` / ``geo-trace`` / ``ipinfo`` requests
typed in natural language and runs the real host tool, so the chat AI never has
to invent hop/latency/ASN data. Strict intent signals (a tool word + a valid
target) are required so normal chatter is never hijacked.
"""

from __future__ import annotations

import re

from app.geotrace import format_geotrace, geotrace
from app.interfaces.formatter import bold, esc, mono
from app.netinfo import asn_lookup, asn_number_lookup, format_asn, format_asn_number
from app.shell import format_server_command, parse_server_request, run_network_cmd
from app.whois import extract_domain, extract_ip

_KIND_RE = re.compile(
    r"(?<![a-z0-9-])("
    r"traceroute[\w-]*|trace-?route\w*|trace\b"
    r"|geo-?trace\w*"
    r"|ping(?:-in|-ing|ing)?\b"
    r"|asn\b|autonomous\b"
    r"|ip-?info\b|ip\s+info\b|info\s+ip\b|ip\s+addr\w*|ip\s+a\b|ifconfig\b"
    r"|(?:cek|lihat|liat\w*|tunjukin|kasih|check|jelasin\w*|jelaskan\w*)\s+(?:rute|route)\b"
    r"|rute(?=\s+ke\b)|route(?=\s+ke\b)"
    r")(?![a-z0-9-])",
    re.I,
)

# config-ish phrasing that must NOT trigger a bare "rute/route ke X" traceroute
_CONFIG_RE = re.compile(
    r"\b(buat\w*|config\w*|configur\w*|tabel\w*|route\s*table\b|route-table|add\w*|tambah\w*)\b",
    re.IGNORECASE,
)

def _normalize_kind(match) -> str | None:
    word = (match.group(1) or "").strip().lower().replace(" ", "")
    if word.startswith("geo") and "trace" in word:
        return "geotrace"
    if word.startswith("traceroute") or word.startswith("trace"):
        return "traceroute"
    if word.endswith("rute") or word.endswith("route"):
        return "traceroute"
    if word.startswith("ping"):
        return "ping"
    if word in ("asn", "autonomous"):
        return "asn"
    if word in ("ipinfo", "ip-info", "infoip", "ipa", "ipaddr", "ipaddress", "ifconfig"):
        return "ipinfo"
    return None


_ASN_NUM_RE = re.compile(r"(?:^|[\s#])(?:AS\s*)?(\d{1,10})\s*(?:\s|$)", re.I)
_ASN_FULL_RE = re.compile(r"^\s*AS\s*(\d{1,10})\s*$", re.I)


def _extract_target(kind: str, text: str) -> str | None:
    ip = extract_ip(text)
    if ip is not None and not ip.startswith("127."):
        return ip
    domain = extract_domain(text)
    if domain is not None:
        return domain
    if kind == "asn":
        m = _ASN_NUM_RE.search(text.lower())
        if m:
            return m.group(1)
    return None


def _target_valid(kind: str, target: str) -> bool:
    if kind == "ipinfo":
        return True
    if kind == "asn":
        if target.isdigit():
            return True
        return extract_ip(target) is not None
    if kind == "geotrace":
        return parse_server_request(f"traceroute {target}") is not None
    return parse_server_request(f"{kind} {target}") is not None


# Stopwords + synonyms for comparing user text with monitor NAMES, so
# "ping ke switch main univ" is scored against the *whole* name instead of
# whichever monitor happens to share the last token ("univ" exists on both
# "ro-univ" and "sw-main univ").
_DIAG_STOPWORDS = {
    "ke", "di", "yang", "itu", "ini", "itu", "sini", "situ", "aja", "dong",
    "deh", "dah", "tolong", "minta", "coba", "cobain", "bro", "bang", "mas",
    "kak", "gua", "gue", "gw", "aku", "saya", "lo", "lu", "kamu", "gan",
    "dll", "bisa", "mau", "sama", "pada", "dari", "untuk", "buat", "lagi",
}
_DIAG_SYNONYMS = {
    "switch": "sw", "switches": "sw", "router": "ro", "routers": "ro",
}


def _name_tokens(text: str) -> set[str]:
    words = set()
    for part in re.split(r"[^a-z0-9]+", (text or "").lower()):
        part = part.strip("_ -")
        if not part or part in _DIAG_STOPWORDS:
            continue
        words.add(_DIAG_SYNONYMS.get(part, part))
    return words


def detect_diag_request(text: str) -> dict | None:
    """Return ``{kind, target}`` for a network-diagnostic request, else None.

    ``kind``: ``traceroute|ping|asn|geotrace|ipinfo``. ``target`` is None only
    for ``ipinfo`` (runs ``ip a`` on the host).
    """
    t = (text or "").strip().strip(" .,!?;:")
    if not t or t.startswith("/"):
        return None
    if len(t) > 160:
        return None

    m = _KIND_RE.search(t)
    if not m:
        asn_only = _ASN_FULL_RE.match(t)
        if asn_only:
            return {"kind": "asn", "target": asn_only.group(1)}
        return None
    kind = _normalize_kind(m)
    if kind is None:
        return None
    if kind == "ipinfo":
        return {"kind": "ipinfo", "target": None}

    # Bare "rute/route ke X" is only a traceroute when it's not route-config talk.
    matched = (m.group(1) or "").lower().replace(" ", "")
    if matched in ("rute", "route") and _CONFIG_RE.search(t):
        return None

    target = _extract_target(kind, t)
    if target is None:
        return None
    if not _target_valid(kind, target):
        return None
    return {"kind": kind, "target": target}


def detect_mention_diag(text: str, monitors: dict[str, str]) -> dict | None:
    """Resolve a diag request that addresses a known MONITOR NAME (not an IP).

    Example: "Tolong trace ke BIOS dong" — "ro-bios" is a monitored target ->
    runs a real traceroute to 103.153.42.237 instead of letting the AI invent
    hops. ``monitors`` maps ``lower_target_name -> target``.

    The intent word itself is masked out before fragment matching, so a monitor
    whose name *contains the tool word* ("Google Ping") can't swallow a request
    meant for another target ("ping ke switch cyber" must resolve to the switch,
    not to 8.8.8.8).
    """
    t = (text or "").strip().strip(" .,!?;:")
    if not t or t.startswith("/") or len(t) > 160:
        return None
    m = _KIND_RE.search(t)
    if not m:
        return None
    kind = _normalize_kind(m)
    if kind is None or kind == "ipinfo":
        return None
    matched = (m.group(1) or "").lower().replace(" ", "")
    if matched in ("rute", "route") and _CONFIG_RE.search(t):
        return None
    low = t.lower()
    # Neutralize the intent word so it cannot be the thing that matches against
    # a monitor's name fragment (e.g. "ping" in the monitor "google ping").
    intent_raw = (m.group(1) or "").strip()
    masked = re.sub(
        rf"(?<![a-z0-9-]){re.escape(intent_raw.lower())}(?![a-z0-9-])",
        " ",
        low,
        count=1,
    )

    user_words = _name_tokens(masked)
    masked_norm = re.sub(r"[^a-z0-9]", "", masked)
    best: dict | None = None
    best_score = 0
    for name, target in (monitors or {}).items():
        key = (name or "").strip().lower()
        if len(key) < 3:
            continue
        score = len(user_words & _name_tokens(key))
        # Explicit full-name mention ("ping ke google ping") outweighs any
        # partial token overlap on other monitors.
        raw_norm = re.sub(r"[^a-z0-9]", "", key)
        if raw_norm in masked_norm:
            score += 50
        if score > best_score:
            best_score = score
            best = {"kind": kind, "target": target}
    if best is not None and _target_valid(kind, best["target"]):
        return best
    return None


async def run_diag(kind: str, target: str | None) -> str:
    """Run a network-diagnostic for real and render the deterministic card."""
    if kind == "ipinfo":
        args = ["ip", "a"]
        result = await run_network_cmd(args)
        return format_server_command(args, result)

    if kind == "geotrace":
        if not (target or "").strip():
            return f"{bold('Geo-Trace')} — kasih host tujuan dong."
        if not parse_server_request(f"traceroute {target}"):
            return (
                f"{bold('Geo-Trace')} — target {mono(esc(target))} gak valid.\n"
                f"Contoh: {mono('/geo-trace google.com')} atau {mono('/geo-trace 8.8.8.8')}"
            )
        geo = await geotrace(target)
        return format_geotrace(geo)

    if kind == "asn":
        ip = extract_ip(target or "")
        if ip is not None and not ip.startswith("127."):
            info = await asn_lookup(ip)
            if info is None:
                return f"{bold('ASN')} — {mono(esc(ip))} gagal di-resolve."
            return format_asn(ip, info)
        raw = (target or "").strip().lstrip("ASas").strip()
        if not raw.isdigit():
            return f"{bold('ASN')} — kasih IP publik atau AS number dong."
        asn_num = int(raw)
        info = await asn_number_lookup(asn_num)
        if info is None:
            return f"{bold('ASN')} — {mono(f'AS{asn_num}')} gagal di-resolve via RDAP."
        return format_asn_number(asn_num, info)

    if kind in ("traceroute", "ping"):
        args = parse_server_request(f"{kind} {target}")
        if not args:
            return (
                f"{bold(kind.capitalize())} — target {mono(esc(target))} gak valid.\n"
                f"Contoh: {mono(f'/{kind} 8.8.8.8')} atau {mono(f'/{kind} google.com')}"
            )
        result = await run_network_cmd(args)
        return format_server_command(args, result)

    return f"{bold('Diag')} — request gak dikenal."