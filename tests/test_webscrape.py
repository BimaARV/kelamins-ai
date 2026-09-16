"""Tests for commandless web scraping (app/webscrape.py).

Network calls are avoided: parse_html is pure HTML extraction and
validate_scrape_url runs with a fake resolver.
"""

from __future__ import annotations

import pytest

from app.webscrape import (
    detect_scrape_request,
    extract_ip_literal,
    extract_url,
    format_scrape,
    parse_html,
    validate_scrape_url,
)

_HTML = """<html><head>
<title>Berita Teknologi Terbaru</title>
<meta name="description" content="Ringkasan artikel teknologi 2026.">
</head><body>
<nav><a href="/internal">menu</a></nav>
<script>bad()</script>
<p>KELA meluncurkan fitur monitoring jaringan yang keren banget.</p>
<div>Dibaca 1200 kali hari ini.</div>
<a href="https://example.com/posts/1">Baca artikel 1</a>
<a href="http://localhost/">jangan scrape ini</a>
<a href="mailto:x@example.com">email</a>
</body></html>"""


def test_extract_url_basic():
    assert extract_url("coba scrape https://example.com/foo ya") == "https://example.com/foo"


def test_extract_url_none():
    assert extract_url("ga ada url di sini") is None


def test_detect_scrape_request_ok():
    url = detect_scrape_request("scrape https://example.com/foo isinya apa")
    assert url == "https://example.com/foo"


def test_detect_scrape_request_ambil_isi():
    url = detect_scrape_request("ambil isi dari https://cnnindonesia.com/teknologi")
    assert url == "https://cnnindonesia.com/teknologi"


def test_detect_scrape_request_no_intent():
    assert detect_scrape_request("coba buka https://example.com") is None


def test_extract_ip_literal():
    assert extract_ip_literal("8.8.8.8") == "8.8.8.8"
    assert extract_ip_literal("8.8.8") is None
    assert extract_ip_literal("999.1.1.1") is None
    assert extract_ip_literal("example.com") is None


def test_validate_scrape_url_public():
    assert validate_scrape_url("https://example.com/x", resolver=lambda h: "93.184.216.34") == (
        "https://example.com/x"
    )


def test_validate_scrape_url_private_ip_rejected():
    with pytest.raises(ValueError):
        validate_scrape_url("http://192.168.1.5/admin")
    with pytest.raises(ValueError):
        validate_scrape_url("http://10.0.0.1/")


def test_validate_scrape_url_host_resolves_private_rejected():
    with pytest.raises(ValueError):
        validate_scrape_url("https://example.com/x", resolver=lambda h: "10.0.0.9")


def test_validate_scrape_url_host_resolves_public_ok():
    assert validate_scrape_url(
        "https://example.com/x", resolver=lambda h: "1.2.3.4"
    ) == "https://example.com/x"


def test_validate_scrape_url_localhost_rejected():
    with pytest.raises(ValueError):
        validate_scrape_url("http://localhost/status")
    with pytest.raises(ValueError):
        validate_scrape_url("http://metadata.google.internal/latest")
    with pytest.raises(ValueError):
        validate_scrape_url("http://169.254.169.254/latest/meta-data")


def test_validate_scrape_url_bad_scheme():
    with pytest.raises(ValueError):
        validate_scrape_url("ftp://example.com/file")


def test_parse_html_extraction():
    data = parse_html(_HTML, "https://example.com/")
    assert data["title"] == "Berita Teknologi Terbaru"
    assert data["description"] == "Ringkasan artikel teknologi 2026."
    assert "monitoring jaringan" in data["text"]
    assert "bad()" not in data["text"]


def test_parse_html_drops_blocked_and_mailto_links():
    data = parse_html(_HTML, "https://example.com/")
    urls = [link["url"] for link in data["links"]]
    assert "https://example.com/posts/1" in urls
    assert not any("localhost" in u for u in urls)
    assert not any(u.startswith("mailto:") for u in urls)


def test_format_scrape_error():
    out = format_scrape({"url": "https://x.test", "error": "timed out"})
    assert "gak bisa di-scrape" in out
    assert "timed out" in out


def test_format_scrape_escapes_html():
    data = {
        "url": "https://example.com/",
        "title": '<script>alert(1)</script> berita',
        "description": "desc",
        "text": "teks & lainnya",
        "links": [{"url": "https://example.com/x", "label": "<b>label</b>"}],
    }
    out = format_scrape(data)
    assert "<script>" not in out
    assert "&amp;" in out
    assert "<a href=" in out