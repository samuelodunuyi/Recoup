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

    # Storage.
    database_url: str = "postgresql://recoup:recoup@localhost:5432/recoup"

    # App.
    demo_mode: bool = True


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()
