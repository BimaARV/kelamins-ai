"""On-demand web scraping from free-text chat (no slash command needed).

The bot intercepts requests like "scrape https://...", "ambil isi dari link
ini ...", "fetch halaman ..." and returns a deterministic, HTML-escaped summary
(title, description, text, links). Facts come from the page - the AI layer is
never involved, so nothing is hallucinated.

Security (SSRF guard): the bot container runs on the host network, so scraping
a private/loopback/link-local target would hit the machine itself. Every URL's
host is resolved and rejected unless it is a public routable IP (or a domain
that only resolves to public IPs).
"""

from __future__ import annotations

import logging
import re
import socket
import urllib.parse
from typing import Any

import httpx

from app.config import settings
from app.interfaces.formatter import bold, esc, mono
from app.whois import is_public_ip

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SCRAPE_INTENT = re.compile(
    r""
    r"\b(scrape|scraping|scrap|webfetch|fetch|ambil|ekstrak|parsing|parse|baca|ringkas|lihat)\b"
    r"[^\n]{0,60}\b(?:isi|konten|judul|artikel|halaman|web|website|link|url)\b"
    r"|\b(?:isi|konten|judul|artikel|halaman)\b[^\n]{0,40}\b(?:dari|di|web|website|url|link)\b"
    r"|\bscrap(?:e|ing)\b",
    re.IGNORECASE,
)

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def extract_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?)]}>\"'")


def detect_scrape_request(text: str) -> str | None:
    """Return the URL when the user clearly asks to scrape, else None."""
    url = extract_url(text)
    if url is None:
        return None
    if not _SCRAPE_INTENT.search(text):
        return None
    return url


_SSRF_BLOCK_WORDS = (
    "localhost",
    "127.0.0.1",
    "metadata",
    "169.254.169.254",
)


def _host_from_url(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("cuma URL http/https yang boleh")
    if parsed.username or parsed.password:
        raise ValueError("URL dengan kredensial gak diterima")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL gak punya host")
    return host.lower()


def validate_scrape_url(raw: str, resolver=socket.gethostbyname) -> str:
    """Return a normalized public-web URL or raise ValueError (friendly msg)."""
    url = (raw or "").strip()
    if not url:
        raise ValueError("URL kosong.")
    host = _host_from_url(url)

    # Host is already an IP literal.
    candidate_ip = extract_ip_literal(host)
    if candidate_ip is not None:
        if not is_public_ip(candidate_ip):
            raise ValueError("host-nya IP privat/loopback - ditolak (SSRF guard).")
        return url

    if any(block in host for block in _SSRF_BLOCK_WORDS):
        raise ValueError("host lokal/metadata ditolak (SSRF guard).")

    try:
        resolved = resolver(host)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"host gak bisa di-resolve: {host}") from exc
    if not is_public_ip(resolved):
        raise ValueError(f"host {host} gak resolve ke IP publik - ditolak (SSRF guard).")
    return url


def extract_ip_literal(host: str) -> str | None:
    parts = host.split(".")
    if len(parts) != 4:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 or n > 255 for n in nums):
        return None
    return ".".join(parts)


def _collapse_ws(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text[:limit]


async def scrape_url(
    raw_url: str,
    *,
    max_bytes: int | None = None,
    timeout: float | None = None,
    follow_redirects: bool = True,
) -> dict[str, Any]:
    """Fetch and parse a public webpage into a deterministic summary dict."""
    url = validate_scrape_url(raw_url)
    max_bytes = max_bytes or int(settings.web_scrape_max_bytes or 524_288)
    timeout = timeout or float(settings.http_timeout_seconds or 20.0)
    headers = {
        "User-Agent": _UA,
        "Accept-Language": "id,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects, max_redirects=5
        ) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    return {
                        "url": url, "final_url": str(resp.url),
                        "status_code": resp.status_code,
                        "error": f"HTTP {resp.status_code}",
                    }
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        truncated = True
                        break
                body = b"".join(chunks)
        final_url = str(resp.url)
        charset = resp.charset_encoding or "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        info = parse_html(text, url)
        info["url"] = url
        info["final_url"] = final_url
        info["status_code"] = resp.status_code
        info["truncated"] = truncated
        return info
    except httpx.TimeoutException:
        return {"url": raw_url, "error": "timed out fetching halaman"}
    except ValueError as exc:
        return {"url": raw_url, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape failed for %s: %s", raw_url, exc)
        return {"url": raw_url, "error": str(exc)[:300]}


def parse_html(html: str, base_url: str) -> dict[str, Any]:
    """Pure HTML extraction (testable without network)."""
    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup(html or "", "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = " ".join(soup.title.string.split())
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and isinstance(og, Tag) and og.get("content"):
            title = str(og["content"]).strip()

    description = ""
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and isinstance(meta, Tag) and meta.get("content"):
            description = str(meta["content"]).strip()
            break

    for node in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        node.decompose()

    page_text = _collapse_ws(
        soup.get_text(" ", strip=True),
        int(settings.web_scrape_text_chars or 1800),
    )

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        parsed = urllib.parse.urljoin(base_url, href)
        host = urllib.parse.urlparse(parsed).netloc.lower()
        if any(block in host for block in _SSRF_BLOCK_WORDS):
            continue
        if parsed in seen or not parsed.startswith(("http://", "https://")):
            continue
        seen.add(parsed)
        label = _collapse_ws(" ".join(a.get_text(" ", strip=True).split()), 60)
        links.append({"url": parsed, "label": label})
        if len(links) >= int(settings.web_scrape_max_links or 6):
            break

    return {
        "title": title or "(tanpa judul)",
        "description": description,
        "text": page_text,
        "links": links,
    }


def format_scrape(data: dict[str, Any]) -> str:
    """HTML card for a scrape result (all values escaped)."""
    if data.get("error"):
        return (
            f"{bold('WEB')} — {mono(esc(data.get('url', '')))} gak bisa di-scrape.\n"
            f"Alasan: {esc(str(data['error'])[:200])}"
        )
    url = data.get("url", "")
    host = urllib.parse.urlparse(url).netloc or url
    lines = [f"{bold('WEB')} — {mono(esc(host))}"]
    title = data.get("title") or "(tanpa judul)"
    lines.append(esc(str(title)))
    desc = data.get("description")
    if desc:
        lines.append(f"\n{esc(str(desc)[:300])}")
    text = data.get("text")
    if text:
        lines.append(f"\n{esc(str(text))}")
    links = data.get("links") or []
    if links:
        lines.append(f"\n{bold('Link terkait')} (top {len(links)}):")
        for link in links:
            label = (link.get("label") or link.get("url") or "").strip()
            lines.append(f"  • <a href=\"{esc(link.get('url', ''))}\">{esc(label[:60])}</a>")
    if data.get("truncated"):
        lines.append("\n(Konten kepotong — halamannya gede, gua ambil awalnya aja.)")
    lines.append(f"\nSumber: <a href=\"{esc(url)}\">{esc(url[:80])}</a>")
    return "\n".join(lines)