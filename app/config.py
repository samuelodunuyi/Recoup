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
    # Global LLM budget across all conversations over a rolling 24h. New turns are
    # refused past this, so rotating conversation ids can't run up an unbounded bill.
    max_daily_cost_usd: float = 5.00

    # Auth: protected (write/admin) endpoints require this X-API-Key. Fail-closed:
    # when unset, those endpoints are disabled (503). Rate limit is requests/minute
    # per client IP.
    recoup_api_key: str = ""
    rate_limit_per_minute: int = 60
    # Behind a reverse proxy (Render/Railway/Fly), take the client IP from the
    # right-most X-Forwarded-For entry (the one the proxy appended). Leave off when
    # the app is reachable directly, or clients could spoof their IP.
    trust_proxy_headers: bool = False

    # Webhook HMAC secret for /events/*. Fail-closed: unset = webhook disabled (503).
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
    # Empty handoff_email also disables email.
    handoff_email: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "recoup@localhost"

    # ─── Payment processors (all optional; demo links/reminders when unset) ───
    # Paystack signs webhooks with the account secret key, so this one setting
    # enables both the /webhooks/paystack endpoint and real payment links.
    paystack_secret_key: str = ""
    flutterwave_secret_key: str = ""
    # The "secret hash" set in the Flutterwave dashboard; sent back as `verif-hash`.
    flutterwave_secret_hash: str = ""
    # Where the hosted checkout sends the customer afterwards (Flutterwave requires it).
    payment_redirect_url: str = ""

    # ─── WhatsApp Cloud API (optional; replies are only returned over HTTP when unset) ───
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""       # verifies inbound webhook signatures
    whatsapp_verify_token: str = ""     # echoed during Meta's webhook subscription check
    # Approved template for the business-initiated first message. Its body must take
    # three parameters: {{1}} customer name, {{2}} amount, {{3}} payment link.
    whatsapp_opening_template: str = ""
    whatsapp_template_language: str = "en"
    whatsapp_api_version: str = "v21.0"
    # Prefix for local-format numbers ("0803…") when normalising phone numbers.
    default_country_code: str = "234"

    # ─── Scheduled retries ───
    default_retry_days: int = 3         # when the customer agrees to "later" with no date
    retry_poll_seconds: int = 60        # how often the retry worker checks for due retries

    # App.
    demo_mode: bool = True
    # Public URL of this deployment (e.g. https://recoup.onrender.com), used to make
    # demo checkout links absolute so they work outside the browser demo.
    public_base_url: str = ""


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()
