"""News RSS collector offline tests (fixture bytes, no network)."""

from app.collectors.news.rss import parse_feed


def test_parse_feed_normalizes_entries(sample_rss_bytes):
    items = parse_feed(sample_rss_bytes, source_id=7)
    assert len(items) == 2  # the javascript: URL entry is dropped
    first = items[0]
    assert first["source_id"] == 7
    assert first["title"] == "Gempa guncang Maluku Barat Daya"
    assert first["url"] == "https://example.com/a/gempa-maluku"
    assert first["author"] == "Reporter"
    assert first["content_hash"]
    assert len(first["content_hash"]) == 64


def test_parse_feed_skips_invalid_urls(sample_rss_bytes):
    items = parse_feed(sample_rss_bytes, source_id=1)
    urls = [i["url"] for i in items]
    assert all(u.startswith("http") for u in urls)
    assert "javascript:alert(1)" not in urls


def test_parse_feed_empty_payload():
    assert parse_feed(b"", source_id=1) == []