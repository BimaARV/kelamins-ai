"""BMKG location resolver tests against the shipped Kemendagri index."""

from app.collectors.weather.locations import (
    get_index,
    resolve_location,
    search_locations,
)


def test_index_loaded():
    index = get_index()
    assert len(index.codes) > 90000
    assert index.codes["31.71.01.1001"] == "Gambir"


def test_adm4_code_query_is_direct():
    hit = resolve_location("31.71.01.1001")
    assert hit is not None
    assert hit["code"] == "31.71.01.1001"
    assert hit["village"] == "31.71.01.1001"


def test_cimanggis_resolves_to_depok_village():
    hit = resolve_location("Cimanggis")
    assert hit is not None
    assert hit["code"].startswith("32.76.02")
    assert hit["village"] == "32.76.02.1007"
    assert "depok" in hit["label"].lower()


def test_tapos_prefers_depok():
    results = search_locations("Tapos", limit=10)
    assert results[0]["village"] == "32.76.10.1001"
    assert "depok" in results[0]["label"].lower()


def test_kota_depok_expands_to_village():
    hit = resolve_location("Depok")
    assert hit is not None
    assert hit["village"].startswith("32.76.")


def test_unknown_location_returns_none():
    assert resolve_location("qwertyuioplkjhg") is None
    assert search_locations("") == []