"""Schema structure tests against an in-memory SQLite render.

Confirms every table, its unique constraints, and key columns required by the
architecture spec section 21 exist without touching MariaDB.
"""

import sqlalchemy as sa

from app.db.base import Base
import app.db.models  # noqa: F401 - register

TABLES = {
    "sources",
    "articles",
    "events",
    "event_articles",
    "earthquakes",
    "network_targets",
    "network_checks",
    "ai_requests",
    "ai_interpretations",
    "weather_locations",
    "weather_forecasts",
    "alerts",
    "alert_messages",
    "alert_deliveries",
    "documents",
}


def _render_metadata() -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)


def test_all_spec_tables_present():
    _render_metadata()
    assert set(Base.metadata.tables) == TABLES


def test_articles_unique_url():
    _render_metadata()
    table = Base.metadata.tables["articles"]
    unique = {c.name for c in table.constraints if isinstance(c, sa.UniqueConstraint)}
    assert "uq_articles_url" in unique


def test_earthquakes_unique_source_external_id():
    _render_metadata()
    table = Base.metadata.tables["earthquakes"]
    names = [
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    ]
    assert "uq_earthquakes_source_external_id" in names


def test_event_articles_pk_composite():
    _render_metadata()
    table = Base.metadata.tables["event_articles"]
    pk = list(table.primary_key.columns)
    assert {c.name for c in pk} == {"event_id", "article_id"}


def test_ai_interpretations_unique_per_event_type():
    _render_metadata()
    table = Base.metadata.tables["ai_interpretations"]
    names = [
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    ]
    assert "uq_ai_interpretations_event_type" in names
    assert {"interpretation_type", "content", "meta", "ai_request_id"} <= set(table.c.keys())


def test_alerts_unique_event_one_thread():
    _render_metadata()
    table = Base.metadata.tables["alerts"]
    names = [
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    ]
    assert "uq_alerts_event_id" in names
    assert {"event_id", "priority", "title", "message_count", "last_signature"} <= set(
        table.c.keys()
    )
    messages = Base.metadata.tables["alert_messages"]
    assert {"alert_id", "kind", "priority", "body", "meta"} <= set(messages.c.keys())
    deliveries = Base.metadata.tables["alert_deliveries"]
    assert {"alert_message_id", "channel", "status", "external_id", "error"} <= set(
        deliveries.c.keys()
    )


def test_required_spec_columns_exist():
    _render_metadata()
    t = Base.metadata.tables
    required = {
        "sources": {"name", "source_type", "base_url", "feed_url", "enabled"},
        "articles": {"source_id", "title", "url", "content", "content_hash", "published_at"},
        "earthquakes": {"external_id", "magnitude", "depth_km", "latitude", "longitude", "source", "raw_data"},
        "network_targets": {"target_type", "target", "port", "interval_seconds", "timeout_seconds"},
        "network_checks": {"target_id", "status", "latency_ms", "checked_at"},
        "ai_requests": {"provider", "key_name", "model", "status", "latency_ms"},
    }
    for table, cols in required.items():
        assert cols <= set(t[table].c.keys()), f"{table} missing columns"


def test_every_table_has_created_at_updated_at():
    _render_metadata()
    t = Base.metadata.tables
    for name in ("sources", "articles", "events", "earthquakes", "network_targets"):
        assert "created_at" in t[name].c
        assert "updated_at" in t[name].c