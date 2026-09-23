# Production features and observability

## What's implemented

| Area | What | Where |
| --- | --- | --- |
| Reliability | Pooled, health-checked DB connections; retry/backoff + provider fallback; JSON-truncation retry; per-turn timeout; request-id tracing | `llm/client.py`, `app/db.py`, `app/recovery.py` |
| Auth | Fail-closed `X-API-Key` on admin endpoints; fail-closed, signature-verified webhooks; proxy-aware per-IP rate limit | `app/auth.py`, `app/main.py` |
| Safety | Prompt-injection guard (message + name) and optional moderation → human handoff; per-conversation and global 24h spend caps; bounded, validated inputs; action-schema validation; XSS-safe UI | `graph/safety.py`, `graph/actions.py`, `app/recovery.py` |
| Concurrency | Per-conversation advisory lock so parallel turns don't clobber memory | `app/db.py` |
| Processor integration | Paystack + Flutterwave webhooks open and close recovery conversations; real hosted-checkout links | `app/payments.py` |
| Channel | WhatsApp Cloud API: template opener, free-text replies, signed inbound webhook routed by phone | `app/whatsapp.py` |
| Retry engine | `SCHEDULE_RETRY` → Postgres queue (`SKIP LOCKED`, safe across workers) → saved-card re-charge or reminder on the agreed day | `app/recovery.py`, `app/db.py` |
| Feedback loop | Outcomes recorded automatically (pending → scheduled/escalated → recovered); manual override via `POST /outcome`; winning strategies folded back into RAG via `POST /playbook` | `app/recovery.py`, `app/main.py` |
| Human handoff | Every escalation queued (`GET /handoffs`), optionally emailed | `app/notify.py` |
| Migrations & scale | Alembic migrations; indexes incl. pgvector HNSW; scheduled retention purge | `migrations/`, `app/db.py` |
| Health & metrics | `/health`, `/ready`, `/metrics`, `/metrics/prometheus`, optional Sentry | `app/main.py` |
| Analytics | Dashboard at `/dashboard`: recovery rate, revenue recovered, cost, funnel | `app/static/dashboard.html` |
| Localisation | English, Nigerian Pidgin, Spanish, French, Swahili | `graph/prompts.py` |
| Compliance | Generated text kept out of logs; retention purge; see [COMPLIANCE.md](../COMPLIANCE.md) | `llm/client.py`, `app/db.py` |
| Testing / CI | Unit, security, recovery-flow and Postgres integration tests in GitHub Actions; nightly eval gate | `tests/`, `.github/workflows/` |

## Observability

Every graph node emits a structured JSON trace line (`recoup.trace`) with a per-turn
`run_id`, request id, conversation id, node name and duration. Every LLM call emits
a line (`recoup.llm`) with provider, model, token counts, latency and cost — never
the generated text, which can contain customer PII. Call metadata is also persisted
(`llm_calls`) and aggregated by `GET /metrics`. The graph is a standard LangGraph
app, so LangSmith can be enabled with its environment variables.
