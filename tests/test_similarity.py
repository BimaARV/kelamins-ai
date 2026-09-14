"""Unit tests for deterministic similarity (no ML)."""

from datetime import datetime, timedelta

from app.event_engine.similarity import (
    article_similarity,
    cosine,
    jaccard,
    tokenize,
)

NOW = datetime(2026, 9, 10, 12, 0, 0)


def test_tokenize_strips_stopwords_and_case():
    tokens = tokenize("Gempa Magnitudo 5.2 mengguncang Maluku Barat Daya")
    assert set(tokens) == {"gempa", "magnitudo", "mengguncang", "maluku", "barat", "daya"}


def test_jaccard_basics():
    assert jaccard(tokenize("gempa maluku"), tokenize("gempa maluku")) == 1.0
    assert jaccard(tokenize("gempa"), tokenize("pemilu")) == 0.0


def test_cosine_basics():
    same = tokenize("gempa berkekuatan 5 2 maluku barat daya")
    assert cosine(same, same) > 0.999
    assert cosine(tokenize("gempa maluku"), tokenize("pemilu serentak")) == 0.0


def test_article_similarity_same_event_is_above_threshold():
    a = {
        "title": "Gempa Magnitudo 5.2 guncang Maluku Barat Daya",
        "description": "Gempa berkekuatan 5.2 menguncang Maluku Barat Daya siang tadi.",
        "published_at": NOW,
    }
    b = {
        "title": "Gempa M5.2 Maluku Barat Daya terasa hingga Ambon",
        "description": "Warga melaporkan guncangan akibat gempa Maluku Barat Daya.",
        "published_at": NOW + timedelta(hours=2),
    }
    assert article_similarity(a, b) >= 0.45


def test_article_similarity_unrelated_is_below_threshold():
    quake = {
        "title": "Gempa Magnitudo 5.2 guncang Maluku Barat Daya",
        "description": "Gempa berkekuatan 5.2 menguncang Maluku Barat Daya.",
        "published_at": NOW,
    }
    election = {
        "title": "Pemilu serentak digelar hari Jumat mendatang",
        "description": "KPU mengumumkan jadwal pemungutan suara pemilu.",
        "published_at": NOW,
    }
    assert article_similarity(quake, election) < 0.45


def test_article_similarity_ignores_time_when_missing():
    a = {"title": "Kebakaran pasar kota", "description": "Api menghanguskan kios pasar."}
    b = {"title": "Kebakaran hanguskan pasar kota", "description": "Kios pasar terbakar.", "published_at": NOW}
    assert article_similarity(a, b) > 0.45