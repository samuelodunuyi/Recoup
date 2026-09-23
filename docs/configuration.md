# Configuration

Everything is configured through environment variables (or a `.env` file — copy
[`.env.example`](../.env.example), which lists every setting with comments).
Only an LLM key is needed to run the demo; everything else is optional.

## Core

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PRIMARY` / `LLM_FALLBACK` | `anthropic` / `openai` | Provider order; the client falls back on error or timeout |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Anthropic model |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI chat model |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Provider credentials; leave the fallback's blank to disable it. The OpenAI key also enables embeddings (`text-embedding-3-small`) and optional moderation |
| `DATABASE_URL` | local Postgres | Postgres + pgvector. Conversation memory, retries, outcomes and the RAG store need it |
| `PUBLIC_BASE_URL` | — | This deployment's URL, so demo checkout links are absolute |

## Security and spend

| Variable | Default | Purpose |
| --- | --- | --- |
| `RECOUP_API_KEY` | — | `X-API-Key` for admin endpoints (`/llm/*`, `/handoffs`, `/metrics`, `/outcome`, `/playbook`). They return 503 while it's unset |
| `WEBHOOK_SECRET` | — | HMAC-SHA512 secret for the processor-neutral `/events/*` webhooks (disabled while unset) |
| `MAX_COST_PER_CONVERSATION` | `0.50` | USD per conversation before handing off to a human |
| `MAX_DAILY_COST_USD` | `5.00` | USD across all traffic per rolling 24h; new turns are refused past it |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per-client-IP request limit |
| `TRUST_PROXY_HEADERS` | `false` | Take the client IP from the proxy-appended `X-Forwarded-For` entry (enable behind Render/Railway/Fly) |
| `ENABLE_MODERATION` | `false` | Run customer messages through OpenAI moderation |
| `DATA_RETENTION_DAYS` | `0` (off) | Purge conversations, logs and events older than N days |

## Integrations

See [Going live](going-live.md) for setup steps.

| Variable | Purpose |
| --- | --- |
| `PAYSTACK_SECRET_KEY` | Enables `/webhooks/paystack`, real checkout links and saved-card retries |
| `FLUTTERWAVE_SECRET_KEY` | Flutterwave checkout links |
| `FLUTTERWAVE_SECRET_HASH` | Verifies `/webhooks/flutterwave` (the hash set in the Flutterwave dashboard) |
| `PAYMENT_REDIRECT_URL` | Where hosted checkout sends the customer afterwards |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Send messages via the WhatsApp Cloud API |
| `WHATSAPP_APP_SECRET` | Verifies inbound WhatsApp webhooks |
| `WHATSAPP_VERIFY_TOKEN` | Echoed during Meta's webhook subscription check |
| `WHATSAPP_OPENING_TEMPLATE`, `WHATSAPP_TEMPLATE_LANGUAGE` | Approved template for the first, business-initiated message |
| `DEFAULT_COUNTRY_CODE` | Prefix for local-format phone numbers (default `234`) |
| `DEFAULT_RETRY_DAYS` | Retry delay when a customer says "later" with no date (default `3`) |
| `RETRY_POLL_SECONDS` | How often the retry worker checks for due retries (default `60`) |

## Operations

| Variable | Purpose |
| --- | --- |
| `SENTRY_DSN` | Optional error tracking |
| `HANDOFF_EMAIL`, `SMTP_*` | Email each human handoff (both `SMTP_HOST` and `HANDOFF_EMAIL` required) |
| `LLM_MAX_TOKENS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | Per-call tuning |
| `REQUEST_TIMEOUT_SECONDS` | Hard deadline for one conversation turn |
