"""KELA AI data models (MariaDB 12).

Mirrors monitoring-bot-architecture.md section 21. Raw data is always
persisted before any AI processing; the AI layer is never a source of truth.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now() -> datetime:
    return datetime.utcnow()


class SourceType(str, enum.Enum):
    rss = "rss"
    api = "api"
    scraper = "scraper"


class ProcessingStatus(str, enum.Enum):
    raw = "raw"
    deduplicated = "deduplicated"
    clustered = "clustered"
    verified = "verified"
    failed = "failed"


class EventType(str, enum.Enum):
    earthquake = "earthquake"
    news = "news"
    network = "network"
    other = "other"


class Confidence(str, enum.Enum):
    high = "high"
    medium = "medium"
    low = "low"


class EventStatus(str, enum.Enum):
    active = "active"
    updated = "updated"
    resolved = "resolved"
    closed = "closed"


class RelationType(str, enum.Enum):
    primary = "primary"
    related = "related"
    duplicate = "duplicate"
    update = "update"


class TargetType(str, enum.Enum):
    ping = "ping"
    tcp = "tcp"
    http = "http"
    dns = "dns"
    snmp = "snmp"


class CheckStatus(str, enum.Enum):
    up = "up"
    down = "down"
    timeout = "timeout"
    error = "error"


class AIRequestType(str, enum.Enum):
    summary = "summary"
    classification = "classification"
    clustering = "clustering"
    verification = "verification"
    other = "other"


class AIRequestStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class AlertPriority(str, enum.Enum):
    """Monitoring alert severity (spec section 14)."""

    p1 = "P1"
    p2 = "P2"
    p3 = "P3"
    p4 = "P4"


class AlertMessageKind(str, enum.Enum):
    """One alert thread per event; messages track the event lifecycle."""

    initial = "initial"
    update = "update"
    severity = "severity"
    resolved = "resolved"


class AlertDeliveryStatus(str, enum.Enum):
    delivered = "delivered"
    skipped = "skipped"
    failed = "failed"


class DocumentType(str, enum.Enum):
    pdf = "pdf"
    docx = "docx"
    txt = "txt"
    markdown = "markdown"
    image = "image"
    other = "other"


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("base_url", name="uq_sources_base_url"),
        Index("ix_sources_feed_url", "feed_url"),
        Index("ix_sources_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source_type: Mapped[SourceType] = mapped_column(
        SAEnum(SourceType, name="source_type_enum", native_enum=True), default=SourceType.rss
    )
    base_url: Mapped[str] = mapped_column(String(500))
    feed_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    articles: Mapped[list[Article]] = relationship(back_populates="source")


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("url", name="uq_articles_url"),
        Index("ix_articles_content_hash", "content_hash"),
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(1000))
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        SAEnum(ProcessingStatus, name="processing_status_enum", native_enum=True),
        default=ProcessingStatus.raw,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    source: Mapped[Source] = relationship(back_populates="articles")
    event_links: Mapped[list[EventArticle]] = relationship(back_populates="article")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_occurred_at", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[EventType] = mapped_column(
        SAEnum(EventType, name="event_type_enum", native_enum=True), default=EventType.news
    )
    title: Mapped[str] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confidence: Mapped[Confidence | None] = mapped_column(
        SAEnum(Confidence, name="confidence_enum", native_enum=True), nullable=True
    )
    status: Mapped[EventStatus] = mapped_column(
        SAEnum(EventStatus, name="event_status_enum", native_enum=True),
        default=EventStatus.active,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    article_links: Mapped[list[EventArticle]] = relationship(back_populates="event")
    earthquakes: Mapped[list[Earthquake]] = relationship(back_populates="event")
    ai_interpretations: Mapped[list[AIInterpretation]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    alert: Mapped[Alert | None] = relationship(back_populates="event", uselist=False)


class EventArticle(Base):
    __tablename__ = "event_articles"
    __table_args__ = (
        UniqueConstraint("event_id", "article_id", name="uq_event_articles"),
        Index("ix_event_articles_article_id", "article_id"),
    )

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[RelationType] = mapped_column(
        SAEnum(RelationType, name="relation_type_enum", native_enum=True),
        default=RelationType.related,
    )
    similarity_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    event: Mapped[Event] = relationship(back_populates="article_links")
    article: Mapped[Article] = relationship(back_populates="event_links")


class Earthquake(Base):
    __tablename__ = "earthquakes"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_earthquakes_source_external_id"),
        Index("ix_earthquakes_occurred_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str] = mapped_column(String(200))
    magnitude: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    depth_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    place: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(100))
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    event: Mapped[Event | None] = relationship(back_populates="earthquakes")


class NetworkTarget(Base):
    __tablename__ = "network_targets"
    __table_args__ = (Index("ix_network_targets_enabled", "enabled"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    target_type: Mapped[TargetType] = mapped_column(
        SAEnum(TargetType, name="target_type_enum", native_enum=True), default=TargetType.ping
    )
    target: Mapped[str] = mapped_column(String(300))
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    checks: Mapped[list[NetworkCheck]] = relationship(back_populates="target")


class NetworkCheck(Base):
    __tablename__ = "network_checks"
    __table_args__ = (Index("ix_network_checks_target_id", "target_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    target_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("network_targets.id", ondelete="CASCADE")
    )
    status: Mapped[CheckStatus] = mapped_column(
        SAEnum(CheckStatus, name="check_status_enum", native_enum=True), default=CheckStatus.error
    )
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    packet_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    target: Mapped[NetworkTarget] = relationship(back_populates="checks")


class AIRequest(Base):
    __tablename__ = "ai_requests"
    __table_args__ = (Index("ix_ai_requests_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(100))
    key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str] = mapped_column(String(100))
    request_type: Mapped[AIRequestType] = mapped_column(
        SAEnum(AIRequestType, name="ai_request_type_enum", native_enum=True),
        default=AIRequestType.other,
    )
    status: Mapped[AIRequestStatus] = mapped_column(
        SAEnum(AIRequestStatus, name="ai_request_status_enum", native_enum=True),
        default=AIRequestStatus.queued,
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AIInterpretation(Base):
    """Latest AI-generated artifact for an event.

    Keeps the AI layer fully traceable: every insight references its request
    (ai_requests), model, key and latency in meta. Unique per (event, type);
    re-processing overwrites the previous artifact instead of splaying rows.
    """

    __tablename__ = "ai_interpretations"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "interpretation_type", name="uq_ai_interpretations_event_type"
        ),
        Index("ix_ai_interpretations_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE")
    )
    interpretation_type: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ai_request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_requests.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    event: Mapped[Event] = relationship(back_populates="ai_interpretations")


class WeatherLocation(Base):
    """BMKG weather forecast location (admin level 4 / village).

    Added per product requirement: KELA must be able to read weather forecasts
    from BMKG for the Jakarta-Depok region. Facts always come from the BMKG
    public API (api.bmkg.go.id), never from AI.
    """

    __tablename__ = "weather_locations"
    __table_args__ = (
        UniqueConstraint("adm4", name="uq_weather_locations_adm4"),
        Index("ix_weather_locations_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    adm1: Mapped[str | None] = mapped_column(String(50), nullable=True)
    adm2: Mapped[str | None] = mapped_column(String(50), nullable=True)
    adm3: Mapped[str | None] = mapped_column(String(50), nullable=True)
    adm4: Mapped[str] = mapped_column(String(50))
    provinsi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kotkab: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kecamatan: Mapped[str | None] = mapped_column(String(200), nullable=True)
    desa: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    forecasts: Mapped[list[WeatherForecast]] = relationship(back_populates="location")


class WeatherForecast(Base):
    """Normalized BMKG forecast rows (raw response kept in raw_data)."""

    __tablename__ = "weather_forecasts"
    __table_args__ = (
        UniqueConstraint("location_id", "forecast_datetime", name="uq_weather_forecasts"),
        Index("ix_weather_forecasts_forecast_datetime", "forecast_datetime"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    location_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("weather_locations.id", ondelete="CASCADE")
    )
    forecast_datetime: Mapped[datetime] = mapped_column(DateTime)
    analysis_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    weather_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weather_description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    weather_description_en: Mapped[str | None] = mapped_column(String(100), nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    precipitation_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_deg: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wind_dir: Mapped[str | None] = mapped_column(String(10), nullable=True)
    wind_to_dir: Mapped[str | None] = mapped_column(String(10), nullable=True)
    wind_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    visibility_text: Mapped[str | None] = mapped_column(String(60), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    location: Mapped[WeatherLocation] = relationship(back_populates="forecasts")


class Alert(Base):
    """One alert thread per event (never per article).

    Mirrors the event lifecycle: initial alert, updates when the event gains
    new sources, severity bumps, and a resolution note. Exact dedup guarantee:
    'alerts.event_id' is unique.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_alerts_event_id"),
        Index("ix_alerts_priority", "priority"),
        Index("ix_alerts_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE")
    )
    priority: Mapped[AlertPriority] = mapped_column(
        SAEnum(AlertPriority, name="alert_priority_enum", native_enum=True),
        default=AlertPriority.p3,
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    title: Mapped[str] = mapped_column(String(1000))
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_signature: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    event: Mapped[Event] = relationship(back_populates="alert")
    messages: Mapped[list[AlertMessage]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )


class AlertMessage(Base):
    """One delivered message inside an alert thread."""

    __tablename__ = "alert_messages"
    __table_args__ = (Index("ix_alert_messages_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alerts.id", ondelete="CASCADE")
    )
    kind: Mapped[AlertMessageKind] = mapped_column(
        SAEnum(AlertMessageKind, name="alert_message_kind_enum", native_enum=True),
        default=AlertMessageKind.initial,
    )
    priority: Mapped[AlertPriority] = mapped_column(
        SAEnum(AlertPriority, name="alert_message_priority_enum", native_enum=True),
        default=AlertPriority.p3,
    )
    title: Mapped[str] = mapped_column(String(1000))
    body: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    alert: Mapped[Alert] = relationship(back_populates="messages")
    deliveries: Mapped[list[AlertDelivery]] = relationship(
        back_populates="alert_message", cascade="all, delete-orphan"
    )


class AlertDelivery(Base):
    """Per-channel send attempt for an alert message (audit trail)."""

    __tablename__ = "alert_deliveries"
    __table_args__ = (Index("ix_alert_deliveries_channel", "channel"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alert_messages.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(30))
    status: Mapped[AlertDeliveryStatus] = mapped_column(
        SAEnum(AlertDeliveryStatus, name="alert_delivery_status_enum", native_enum=True),
        default=AlertDeliveryStatus.failed,
    )
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    alert_message: Mapped[AlertMessage] = relationship(back_populates="deliveries")


class Memory(Base):
    """Persistent assistant memory — actions KELA has done + explicit user notes.

    Keeps the bot self-aware across restarts ('Jarvis feel'): every important
    action (monitoring start/stop, down/up flips, cron, whois, file generation)
    and every explicit 'inget/catat …' request lands here, then gets injected
    into the AI system prompt on each free-text turn.
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_created_at", "created_at"),
        Index("ix_memories_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="note")
    source: Mapped[str] = mapped_column(String(50), default="telegram")
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Document(Base):
    """Uploaded or ingested document with extracted text and metadata.

    The Document Engine does the actual file work; AI decides what to do
    with the content. Processing can continue even if Ollama is down.
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_document_type", "document_type"),
        Index("ix_documents_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(500))
    file_path: Mapped[str] = mapped_column(String(1000))
    document_type: Mapped[DocumentType] = mapped_column(
        SAEnum(DocumentType, name="document_type_enum", native_enum=True),
        default=DocumentType.other,
    )
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    text_content: Mapped[str | None] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"), nullable=True
    )
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(20), default="pending")
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )