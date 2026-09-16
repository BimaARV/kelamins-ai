"""Application configuration loaded from environment (Docker-first)."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    # Database (MariaDB 12). Inside Docker this is mariadb:3306, never localhost.
    database_url: str = Field(
        default="mysql+asyncmy://kela:kela@mariadb:3306/kela", alias="DATABASE_URL"
    )

    # Redis (optional; observability/queue in later phases). Service name, never localhost.
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")

    # Collectors
    bmkg_feed_url: str = Field(
        default="https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json", alias="BMKG_FEED_URL"
    )
    news_poll_interval_seconds: int = Field(default=300, alias="NEWS_POLL_INTERVAL_SECONDS")
    earthquake_poll_interval_seconds: int = Field(
        default=120, alias="EARTHQUAKE_POLL_INTERVAL_SECONDS"
    )
    network_poll_interval_seconds: int = Field(default=60, alias="NETWORK_POLL_INTERVAL_SECONDS")
    http_timeout_seconds: float = Field(default=20.0, alias="HTTP_TIMEOUT_SECONDS")

    news_source_fixtures: str = Field(default="[]", alias="NEWS_SOURCE_FIXTURES")
    news_tech_source_keywords: str = Field(
        default='["teknologi", "tekno", "tech", "techno"]', alias="NEWS_TECH_SOURCE_KEYWORDS"
    )
    news_sport_source_keywords: str = Field(
        default='["olahraga", "sport", "sepakbola", "f1", "motogp", "bola"]',
        alias="NEWS_SPORT_SOURCE_KEYWORDS",
    )
    # News recency: articles older than this many hours are never stored or shown
    # (cleans January-2025 backlog feeds like Antara Olahraga). 0 = no limit.
    news_max_age_hours: int = Field(default=72, alias="NEWS_MAX_AGE_HOURS")
    # News briefing for AI discussion (free-text OOT grounding).
    news_briefing_match_limit: int = Field(default=5, alias="NEWS_BRIEFING_MATCH_LIMIT")
    news_briefing_desc_chars: int = Field(default=300, alias="NEWS_BRIEFING_DESC_CHARS")
    network_target_fixtures: str = Field(default="[]", alias="NETWORK_TARGET_FIXTURES")

    # BMKG weather forecasts (Jakarta-Depok configured via WEATHER_LOCATIONS)
    bmkg_weather_endpoint: str = Field(
        default="https://api.bmkg.go.id/publik/prakiraan-cuaca", alias="BMKG_WEATHER_ENDPOINT"
    )
    weather_poll_interval_seconds: int = Field(default=1800, alias="WEATHER_POLL_INTERVAL_SECONDS")
    weather_locations: str = Field(default="[]", alias="WEATHER_LOCATIONS")
    weather_location_index_path: str = Field(
        default="data/kemendagri_adm.csv", alias="WEATHER_LOCATION_INDEX_PATH"
    )

    # Event Intelligence (Phase 2)
    event_poll_interval_seconds: int = Field(default=60, alias="EVENT_POLL_INTERVAL_SECONDS")
    event_similarity_threshold: float = Field(default=0.45, alias="EVENT_SIMILARITY_THRESHOLD")
    event_max_raw_batch: int = Field(default=200, alias="EVENT_MAX_RAW_BATCH")

    # KELA AI Gateway (Phase 3)
    # Multi-provider model pool: JSON list ordered by priority, e.g.
    #   AI_MODELS=[{"provider":"gemini","base_url":"https://generativelanguage.googleapis.com/v1beta/openai","model":"gemini-2.0-flash"}]
    # base_url/model are optional per entry; they fall back to the single-model
    # settings below (AI_* then OLLAMA_*). Empty AI_MODELS -> single model.
    ai_models: str = Field(default="[]", alias="AI_MODELS")
    ai_provider: str = Field(default="ollama", alias="AI_PROVIDER")
    ai_base_url: str | None = Field(default=None, alias="AI_BASE_URL")
    ai_model: str | None = Field(default=None, alias="AI_MODEL")
    ollama_base_url: str = Field(default="https://ollama.com", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="gemma4:cloud", alias="OLLAMA_MODEL")
    ollama_api_key_1: str | None = Field(default=None, alias="OLLAMA_API_KEY_1")
    ollama_api_key_2: str | None = Field(default=None, alias="OLLAMA_API_KEY_2")
    ollama_api_key_3: str | None = Field(default=None, alias="OLLAMA_API_KEY_3")
    ai_poll_interval_seconds: int = Field(default=60, alias="AI_POLL_INTERVAL_SECONDS")
    ai_max_events_per_cycle: int = Field(default=5, alias="AI_MAX_EVENTS_PER_CYCLE")
    ai_max_tokens: int = Field(default=800, alias="AI_MAX_TOKENS")
    ai_chat_max_tokens: int = Field(default=2048, alias="AI_CHAT_MAX_TOKENS")
    ai_temperature: float = Field(default=0.7, alias="AI_TEMPERATURE")
    ai_timeout_seconds: float = Field(default=120.0, alias="AI_TIMEOUT_SECONDS")
    ai_rate_limit_per_minute: int = Field(default=30, alias="AI_RATE_LIMIT_PER_MINUTE")
    ai_circuit_failure_threshold: int = Field(default=5, alias="AI_CIRCUIT_FAILURE_THRESHOLD")
    ai_circuit_cooldown_seconds: int = Field(default=60, alias="AI_CIRCUIT_COOLDOWN_SECONDS")
    ai_key_cooldown_seconds: int = Field(default=60, alias="AI_KEY_COOLDOWN_SECONDS")

    # Alert Engine (Phase 4)
    alert_poll_interval_seconds: int = Field(default=30, alias="ALERT_POLL_INTERVAL_SECONDS")
    alert_max_events_per_cycle: int = Field(default=10, alias="ALERT_MAX_EVENTS_PER_CYCLE")
    alert_update_min_new_sources: int = Field(
        default=2, alias="ALERT_UPDATE_MIN_NEW_SOURCES"
    )
    alert_update_min_new_articles: int = Field(
        default=5, alias="ALERT_UPDATE_MIN_NEW_ARTICLES"
    )
    alert_update_cooldown_seconds: int = Field(
        default=900, alias="ALERT_UPDATE_COOLDOWN_SECONDS"
    )
    alert_earthquake_significant_magnitude: float = Field(
        default=5.0, alias="ALERT_EARTHQUAKE_SIGNIFICANT_MAGNITUDE"
    )
    alert_earthquake_high_magnitude: float = Field(
        default=4.0, alias="ALERT_EARTHQUAKE_HIGH_MAGNITUDE"
    )

    # Interactive bot (Phase 6 - Telegram)
    telegram_poll_interval_seconds: float = Field(
        default=2.0, alias="TELEGRAM_POLL_INTERVAL_SECONDS"
    )
    telegram_chat_context_limit: int = Field(
        default=8, alias="TELEGRAM_CHAT_CONTEXT_LIMIT"
    )
    telegram_news_push_interval_seconds: float = Field(
        default=10800.0, alias="TELEGRAM_NEWS_PUSH_INTERVAL_SECONDS"
    )
    telegram_news_push_count: int = Field(
        default=3, alias="TELEGRAM_NEWS_PUSH_COUNT"
    )
    telegram_digest_hour: int = Field(
        default=6, alias="TELEGRAM_DIGEST_HOUR"
    )
    cron_poll_interval_seconds: float = Field(
        default=15.0, alias="CRON_POLL_INTERVAL_SECONDS"
    )

    # Alert channels (Telegram / Discord)
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    discord_webhook_url: str | None = Field(default=None, alias="DISCORD_WEBHOOK_URL")

    # Chat-driven network monitoring (IP/domain, down/up Telegram alert)
    monitor_state_ttl_seconds: int = Field(
        default=2_592_000, alias="NETWORK_MONITOR_STATE_TTL_SECONDS"  # 30 hari
    )
    monitor_ids_key_ttl_seconds: int = Field(
        default=31_536_000, alias="NETWORK_MONITOR_IDS_TTL_SECONDS"  # 365 hari
    )
    monitor_name_prompt_ttl_seconds: int = Field(
        default=900, alias="MONITOR_NAME_PROMPT_TTL_SECONDS"  # 15 menit
    )

    # Host executor (Phase 6.6 - root + allowlist): file ops + benign commands
    # through the bot container on the real host root (bind /host-root:rw).
    hostcmd_allowed_roots: str = Field(
        default='["/home", "/tmp", "/var/log"]', alias="HOSTCMD_ALLOWED_ROOTS"
    )

    # Jarvis memory (persistent, golden recorded actions + explicit notes)
    memory_max_items: int = Field(default=200, alias="MEMORY_MAX_ITEMS")
    memory_recall_limit: int = Field(default=10, alias="MEMORY_RECALL_LIMIT")

    # On-demand web scraping (free-text, SSRF-guarded)
    web_scrape_max_bytes: int = Field(default=524_288, alias="WEB_SCRAPE_MAX_BYTES")
    web_scrape_text_chars: int = Field(default=1800, alias="WEB_SCRAPE_TEXT_CHARS")
    web_scrape_max_links: int = Field(default=6, alias="WEB_SCRAPE_MAX_LINKS")

    # Document Engine (Phase 5)
    tesseract_cmd: str = Field(default="/usr/bin/tesseract", alias="TESSERACT_CMD")
    document_storage_path: str = Field(default="storage/documents", alias="DOCUMENT_STORAGE_PATH")
    report_storage_path: str = Field(default="storage/reports", alias="REPORT_STORAGE_PATH")
    image_storage_path: str = Field(default="storage/images", alias="IMAGE_STORAGE_PATH")
    document_max_upload_mb: int = Field(default=20, alias="DOCUMENT_MAX_UPLOAD_MB")
    ocr_languages: str = Field(default="ind+eng", alias="OCR_LANGUAGES")

    def load_fixtures(self, key: str) -> list[dict]:
        try:
            data = json.loads(getattr(self, key))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return []
        return data if isinstance(data, list) else []


settings = Settings()