"""Normalizer tests: canonicalization, content hashing, URL validation."""

from app.normalizer import article_content_hash, canonical_text, canonical_url, is_valid_url


def test_canonical_text_strips_html_and_collapses_space():
    assert canonical_text("Besar   <b>Cita</b>  Kencana") == "besar cita kencana"


def test_canonical_text_empty():
    assert canonical_text(None) == ""
    assert canonical_text("") == ""


def test_content_hash_stable_for_same_content():
    h1 = article_content_hash("Gempa Guncang", "Deskripsi berita.", "Isi artikel panjang")
    h2 = article_content_hash("gempa   guncang", "Deskripsi berita.", "Isi artikel panjang")
    assert h1 == h2


def test_content_hash_differs_for_different_content():
    h1 = article_content_hash("Gempa Guncang", "Deskripsi berita.", "Isi artikel panjang")
    h2 = article_content_hash("Gempa Lain", "Deskripsi berita.", "Isi artikel panjang")
    assert h1 != h2


def test_content_hash_deterministic():
    h1 = article_content_hash("A", "B", "C")
    h2 = article_content_hash("A", "B", "C")
    assert h1 == h2
    assert len(h1) == 64


def test_is_valid_url():
    assert is_valid_url("https://example.com/a")
    assert is_valid_url("http://example.com/a")
    assert not is_valid_url(None)
    assert not is_valid_url("")
    assert not is_valid_url("javascript:alert(1)")
    assert not is_valid_url("ftp://example.com/a")
    assert not is_valid_url("https://has space.com/a")


def test_canonical_url_normalizes_and_drops_fragment():
    assert (
        canonical_url("HTTPS://Example.COM/a?x=1&b=2#frag")
        == "https://example.com/a?b=2&x=1"
    )
    assert canonical_url("not a url") is None