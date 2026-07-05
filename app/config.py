"""Application settings, loaded from environment / .env via pydantic-settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Provider routing. Client tries `llm_primary`, falls back to `llm_fallback`.
    llm_primary: str = "anthropic"
    llm_fallback: str = "openai"

    # Per-provider model selection.
    anthropic_model: str = "claude-opus-4-8"
    openai_model: str = "gpt-4o"

    # Credentials (empty disables that provider).
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Request tuning.
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2          # retries per provider on transient errors (#5)
    request_timeout_seconds: float = 60.0  # hard cap on a /chat turn (#8)

    # Storage.
    database_url: str = "postgresql://recoup:recoup@localhost:5432/recoup"

    # Safety: refuse to keep spending on one conversation past this (USD).
    max_cost_per_conversation: float = 0.50

    # Auth: when set, protected (write/admin) endpoints require this X-API-Key.
    # Empty = open (demo). Rate limit is requests/minute per client IP.
    recoup_api_key: str = ""
    rate_limit_per_minute: int = 60

    # Webhook HMAC secret for /events/* (empty = signature check skipped in demo).
    webhook_secret: str = ""

    # Per-call cost alert threshold (USD) — logs a warning above this (#18).
    max_cost_per_call: float = 0.10

    # Optional content moderation on customer input (#17). Uses OpenAI moderations.
    enable_moderation: bool = False

    # Data retention: purge records older than N days (0 = disabled) (#12).
    data_retention_days: int = 0

    # Observability: optional Sentry error tracking (#13).
    sentry_dsn: str = ""

    # Human handoff email (SMTP). Empty smtp_host = email disabled (handoffs still
    # recorded to the DB). Use a Gmail App Password for smtp_password if using Gmail.
    handoff_email: str = "samuelodunuyi@gmail.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "recoup@localhost"

    # App.
    demo_mode: bool = True


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()
