# Recoup

A conversational, WhatsApp-first failed-payment recovery engine for African
subscription businesses on Paystack and Flutterwave. When a recurring charge
fails, an AI agent runs a personalised recovery conversation — adapting tone,
language (including Pidgin) and timing — and closes the payment in-thread with a
link.

This repository runs in **demo mode on synthetic data**: no live Paystack,
Flutterwave, or WhatsApp credentials are needed to run or demo it.

It is also a deliberate portfolio piece for agentic AI engineering: a real
LangGraph multi-agent graph, a RAG pipeline over a pgvector store, provider-
agnostic LLM access with fallback and cost governance, and an eval harness with a
scorecard.

## What's here

| Layer | Where | What it does |
| --- | --- | --- |
| Multi-agent graph | [graph/](graph/) | LangGraph: Router → Decline-Intelligence (RAG) → Negotiator → Memory, with policy routing and a deterministic escalation branch |
| RAG | [rag/](rag/) | Chunk + embed a decline-code KB and playbook into pgvector; hybrid metadata + vector retrieval |
| Provider abstraction | [llm/client.py](llm/client.py) | One interface, Anthropic + OpenAI backends, fallback routing, token/cost logging |
| Eval harness | [eval/](eval/) | 28 scenarios, routing/action/latency/cost scoring + LLM-as-judge tone check, markdown scorecard |
| API + demo UI | [app/](app/) | FastAPI app, persistent conversation memory, and a fake-WhatsApp browser demo |

## Architecture

```
                                 incoming customer message / failed-payment event
                                                  │
                                                  ▼
                                          ┌──────────────┐
                                          │   Router     │  policy-based routing:
                                          │ (classifier) │  new_failure · already_paid ·
                                          └──────┬───────┘  pay_later · dispute · needs_human
                                                 │
                        needs_human ┌────────────┴────────────┐ else
                                    ▼                         ▼
                            ┌──────────────┐         ┌─────────────────────┐
                            │  Escalate    │         │ Decline-Intelligence│  RAG: retrieve the
                            │ (deterministic)│       │      (RAG)          │  right strategy for this
                            └──────┬───────┘         └──────────┬──────────┘  decline code + processor
                                   │                            ▼                     │
                                   │                  ┌──────────────────┐            │ pgvector
                                   │                  │   Negotiator     │  reply +    │ (playbook_chunks)
                                   │                  │  (responder)     │  structured ▼
                                   │                  └────────┬─────────┘  action JSON
                                   └───────────┬───────────────┘
                                               ▼
                                        ┌──────────────┐
                                        │   Memory     │  append turn, record promises;
                                        └──────┬───────┘  persisted to Postgres across turns
                                               ▼
                            reply + action (SEND_PAYMENT_LINK / SCHEDULE_RETRY / ESCALATE_TO_HUMAN)
```

State (`graph/state.py`) flows through every node and is persisted to Postgres
between turns, so the agent remembers prior promises ("said they'd pay Friday")
and doesn't repeat itself.

## Running it

```bash
cp .env.example .env
# Add ANTHROPIC_API_KEY and/or OPENAI_API_KEY to .env
docker compose up --build

# In another terminal, load the RAG knowledge base into pgvector:
docker compose exec app python -m rag.ingest
```

Then open the demo:

- **Demo UI (fake WhatsApp thread):** <http://localhost:8000/>
- **API docs:** <http://localhost:8000/docs>

```bash
# Liveness (app + database)
curl http://localhost:8000/health

# One recovery turn (creates/continues a conversation by id)
curl -X POST http://localhost:8000/chat -H 'content-type: application/json' -d '{
  "conversation_id": "demo-1", "message": "",
  "customer_name": "Ada", "decline_code": "insufficient_funds",
  "processor": "paystack", "language": "english", "amount": 5000
}'

# Provider abstraction smoke test + per-conversation cost report (admin: needs
# RECOUP_API_KEY set in .env; admin endpoints return 503 while it's unset)
curl -X POST http://localhost:8000/llm/ping -H "X-API-Key: $RECOUP_API_KEY" \
  -H 'content-type: application/json' -d '{"message":"hi"}'
curl http://localhost:8000/llm/cost -H "X-API-Key: $RECOUP_API_KEY"
```

## The eval harness

```bash
docker compose exec app python -m eval.runner            # full run incl. LLM judge
docker compose exec app python -m eval.runner --no-judge # faster / cheaper
```

It runs all 28 scenarios ([eval/scenarios.json](eval/scenarios.json)) through the
graph and scores **routing accuracy**, **action correctness**, **latency**,
**cost** (from the token logging), and **tone/language** (LLM-as-judge, including
"did it use Pidgin when expected"). Output:
[eval/scorecard.md](eval/scorecard.md) (human-readable) and `eval/last_run.json`
(machine-readable, for before/after diffs).

> The scorecard is generated on first run (it needs API keys). The intended
> workflow is: run → read the failures → adjust prompts in
> [graph/prompts.py](graph/prompts.py) → re-run → capture the delta.

### Results: harness-driven improvement (86% → 100%)

The first run scored **86% action correctness / 96% routing**. Reading the failures
drove two rounds of fixes:

1. **Disputes** routed correctly but the Negotiator sometimes sent a payment link
   instead of escalating → routed `dispute` straight to the deterministic Escalate
   node and tightened the Negotiator prompt. Action correctness 86% → 96%.
2. **Greetings and unclear messages were being escalated to a human** (a plain
   "hello" triggered a handoff), and one affirmative follow-up ("ok I'm ready")
   misrouted → reclassified greetings/unclear as continuing the conversation and
   affirmatives as `pay_later`. Routing and action reached **100%**.

| Metric | v1 | v2 | current |
| --- | --- | --- | --- |
| **Routing accuracy** | 96% | **100%** | **100%** |
| **Action correctness** | **86%** | **100%** | **100%** |
| Tone (LLM judge) | 0.86 | 0.86 | 0.88 |
| Language pass rate (Pidgin) | 100% | 100% | 100% |
| Avg cost / conversation | $0.0103 | $0.0087 | $0.0115 |

The current column is after adding scheduled retries and processor triggers, which
lengthened the Negotiator prompt (date, trigger, payment status, `retry_at`) — hence
the cost increase. A rerun after that change flagged "later" routing to `new_failure`
and a flat reply when a customer came back as promised; both were fixed in the prompt.

Run on `claude-opus-4-8`, 28 scenarios (current run without a database, i.e. built-in
retrieval strategies, as in the nightly CI gate). The v1 baseline is preserved at
[eval/scorecard_v1.md](eval/scorecard_v1.md); the current run is
[eval/scorecard.md](eval/scorecard.md). This is the "read the failures, fix the
prompts/graph, re-run" loop the eval harness exists for.

## Observability

Every node emits a structured JSON trace line (`recoup.trace`) with a per-turn
`run_id`, conversation id, node name, and duration. Every LLM call emits a
structured line (`recoup.llm`) with provider, model, token counts, latency, and
cost. Together they trace each agent run end-to-end. To switch to LangSmith later,
set its env vars — the graph is a standard LangGraph app.

## Configuration

All via `.env` (see [.env.example](.env.example)):

| Variable | Purpose |
| --- | --- |
| `LLM_PRIMARY` / `LLM_FALLBACK` | Provider order (`anthropic` \| `openai`) |
| `ANTHROPIC_MODEL` | Default `claude-opus-4-8` |
| `OPENAI_MODEL` | Default `gpt-4o` |
| `DATABASE_URL` | Postgres (host `db` inside compose). Memory, retries and outcomes need it |
| `RECOUP_API_KEY` | Admin endpoints' `X-API-Key`; they're disabled while it's empty |
| `MAX_COST_PER_CONVERSATION` / `MAX_DAILY_COST_USD` | Spend caps (per conversation / all traffic per rolling 24h) |
| `PAYSTACK_SECRET_KEY` | Enables `/webhooks/paystack`, real checkout links and saved-card retries |
| `FLUTTERWAVE_SECRET_KEY` / `FLUTTERWAVE_SECRET_HASH` | Flutterwave checkout links / `/webhooks/flutterwave` verification |
| `WHATSAPP_*` | WhatsApp Cloud API channel (see below) |
| `WEBHOOK_SECRET` | HMAC secret for the processor-neutral `/events/*` webhooks |
| `PUBLIC_BASE_URL` | This deployment's URL, so demo checkout links are absolute |

## Going live: processors and WhatsApp

Every integration is off until its keys are set; until then the app runs in demo
mode (browser thread, simulated checkout at `/demo/checkout/…`, reminders instead of
card re-charges). The full lifecycle once connected:

```
Paystack invoice.payment_failed ─┐                        ┌─ WhatsApp: opening template
Flutterwave charge.completed ────┼─▶ open conversation ───┤   (name, amount, checkout link)
POST /events/payment-failed ─────┘   (background turn)    └─ outcome: pending
                                              │
         customer replies on WhatsApp ───────▶ turn ─▶ reply on WhatsApp
                                              │
                    SCHEDULE_RETRY ──▶ retry queue ──(due)──▶ re-charge saved card (Paystack)
                                                              or remind with a fresh link
                                              │
Paystack charge.success / Flutterwave successful ─▶ outcome: recovered, retries cancelled,
POST /events/payment-succeeded                       thank-you message
```

1. **Paystack.** Set `PAYSTACK_SECRET_KEY` (test mode works) and point the dashboard's
   webhook URL at `https://<host>/webhooks/paystack`. Paystack signs webhooks with the
   secret key, so nothing else is needed. The customer's email comes from the event and
   is required to create checkout links.
2. **Flutterwave.** Set `FLUTTERWAVE_SECRET_KEY`, choose a secret hash in the dashboard
   and set it as `FLUTTERWAVE_SECRET_HASH`, and use `https://<host>/webhooks/flutterwave`.
   Set `PAYMENT_REDIRECT_URL` for the post-checkout page.
3. **WhatsApp Cloud API.** From a Meta Business app: `WHATSAPP_ACCESS_TOKEN`,
   `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, and any `WHATSAPP_VERIFY_TOKEN`;
   subscribe the webhook to `https://<host>/webhooks/whatsapp` (messages field).
   WhatsApp only lets a business open a conversation with an **approved template**:
   create one whose body takes `{{1}}` name, `{{2}}` amount, `{{3}}` link, and set its
   name as `WHATSAPP_OPENING_TEMPLATE`. Replies after the customer answers are free text
   written by the agent. Customers are matched to conversations by phone number, taken
   from the processor event (`DEFAULT_COUNTRY_CODE` normalises local numbers).

## Engineering decisions and tradeoffs

- **LangGraph over a single prompt.** Recovery is a stateful, multi-turn process
  with distinct concerns — classification, knowledge retrieval, response
  generation, memory. Modelling it as a graph makes each concern an independently
  testable node, makes routing explicit and auditable (the `needs_human` branch
  skips the LLM entirely and escalates deterministically), and lets the agent
  re-enter and re-route as the conversation continues. A single mega-prompt would
  blur these and make failures hard to localise — which is exactly what the eval
  harness needs to pinpoint.
- **Chunking and embedding choice.** The decline-code KB is chunked one chunk per
  `## ` heading. Decline-code knowledge is naturally atomic — each code's recovery
  guidance is a self-contained unit — so heading-based chunking keeps each
  retrievable chunk whole and on-topic rather than splitting a strategy mid-thought.
  Metadata (decline code + processors) is parsed from each heading so retrieval can
  **filter before it ranks**. Embeddings use `text-embedding-3-small` (1536 dims):
  cheap and fast, and more than accurate enough to rank a few dozen short chunks.
- **Hybrid, degradable retrieval.** Retrieval filters by decline code + processor,
  then ranks by vector similarity. If no OpenAI key is configured, it falls back to
  metadata-only ranking; if the store is empty, the node falls back to hardcoded
  strategies — so the demo works at every level of setup.
- **Provider-agnostic with fallback.** One `LLMClient.complete()` interface
  normalises Anthropic and OpenAI; fallback routing is built in, not bolted on.
  Structured actions use prompt-based JSON parsing rather than each provider's
  native format, so the fallback path stays byte-for-byte identical across
  providers.
- **Cost/token governance from day one.** Every call is priced and logged, so the
  eval harness reports cost-per-conversation alongside accuracy — the basis for a
  truthful "v1 scored X% at $Y/conversation, v2 scored Z%" story.

## Production features

Beyond the core demo, the following are implemented:

| Area | What | Where |
| --- | --- | --- |
| Reliability | Pooling (health-checked), retry/backoff + provider fallback, JSON-truncation retry, per-turn timeout, request-id tracing, idempotent + HMAC-verified webhook | `llm/client.py`, `app/db.py`, `app/main.py` |
| Auth | Fail-closed `X-API-Key` on admin endpoints (disabled until `RECOUP_API_KEY` is set); fail-closed webhook signature check; proxy-aware per-IP rate limit | `app/auth.py`, `app/main.py` |
| Safety | Prompt-injection guard (message + name) + optional OpenAI moderation → handoff; per-conversation **and** global 24h spend caps; bounded, validated inputs; action-schema validation; XSS-safe demo UI | `graph/safety.py`, `graph/actions.py`, `app/main.py` |
| Concurrency | Per-conversation advisory lock so parallel turns don't clobber memory | `app/db.py` |
| Migrations & scale | Alembic migrations; indexes incl. pgvector HNSW; scheduled retention purge | `migrations/`, `app/db.py` |
| Health & metrics | `/health` (liveness), `/ready` (readiness), `/metrics` (JSON), `/metrics/prometheus`, optional Sentry | `app/main.py` |
| Human handoff | Every escalation recorded to a `handoffs` queue (`GET /handoffs`); SMTP email when configured | `app/notify.py`, `app/db.py` |
| Observability | Per-call metadata persisted (`llm_calls`); `GET /metrics` aggregates cost/latency/provider | `app/db.py` |
| Processor integration | Paystack + Flutterwave webhooks (verified, idempotent) open and close recovery conversations; real hosted-checkout links | `app/payments.py`, `app/main.py` |
| Channel | WhatsApp Cloud API: template opener, free-text replies, signed inbound webhook routed by phone | `app/whatsapp.py` |
| Retry engine | `SCHEDULE_RETRY` → Postgres queue (SKIP LOCKED, multi-worker safe) → saved-card re-charge or reminder on the agreed day | `app/recovery.py`, `app/db.py` |
| Feedback loop | Outcomes recorded automatically (pending → scheduled/escalated → recovered on payment); manual override via `POST /outcome`; fold winning strategies back into RAG (`POST /playbook`) | `app/recovery.py`, `app/main.py` |
| Analytics | Dashboard at `/dashboard` (recovery rate, revenue recovered, cost, funnel) | `app/static/dashboard.html` |
| Localisation | English, Nigerian Pidgin, Spanish, French, Swahili | `graph/prompts.py` |
| PII/compliance | Generated text kept out of logs; data-retention purge; posture in [COMPLIANCE.md](COMPLIANCE.md) | `llm/client.py`, `app/db.py` |
| Testing/CI | 88 pytest tests (unit, security, recovery flow, DB integration) run in GitHub Actions | `tests/`, `.github/workflows/ci.yml` |

## Known limitations / what I'd do for production

- **Integrations verified against documented payloads, not live accounts.** The
  Paystack, Flutterwave and WhatsApp adapters are built and tested against their
  documented webhook/API shapes with synthetic data; they haven't yet been exercised
  against real merchant accounts, which would be the first step before real customers.
- **Retry timing.** Retries fire at 08:00 UTC on the day the customer named (the model
  resolves "Friday" to a date and it's validated to fall within 60 days). A production
  system would learn per-customer payday patterns and respect quiet hours per timezone.
- **Only Paystack re-charges saved cards.** Flutterwave retries send a reminder with a
  fresh link instead of a tokenized charge.
- **Vector DB scaling.** pgvector with an HNSW index is right for this size; at much
  larger scale I'd consider a dedicated vector DB.
- **Multi-tenancy.** API-key auth and rate limiting exist, but there's no per-merchant
  isolation, onboarding, or billing yet.
- **Eval coverage.** 28 scenarios is a credible start (now gated nightly in CI via
  `.github/workflows/eval.yml`); production would grow the set from real (anonymised)
  conversations.

## Deploying (Render blueprint)

A [`render.yaml`](render.yaml) blueprint provisions the web service **and** a free
Postgres (with `pgvector`):

1. Push this repo to GitHub.
2. In Render: **New + → Blueprint**, select the repo. It reads `render.yaml`.
3. Set `ANTHROPIC_API_KEY` (and optionally `OPENAI_API_KEY`) in the dashboard —
   they're marked `sync:false` so they're never committed. `RECOUP_API_KEY` is
   generated automatically (copy it from the dashboard to call admin endpoints);
   set `WEBHOOK_SECRET` to enable the payment webhook. The public demo is capped at
   `MAX_DAILY_COST_USD` of LLM spend per rolling 24h.
4. The knowledge base is loaded automatically on first startup when the store is
   empty (`python -m rag.ingest` from the service **Shell** reloads it manually).
5. Health check: `GET /health`. Demo: `/`. Dashboard: `/dashboard`.
6. Optional: set `PUBLIC_BASE_URL` to the service URL, then connect processors and
   WhatsApp as described in [Going live](#going-live-processors-and-whatsapp).

A `Procfile` is included for Railway/Fly/Heroku-style platforms; the `Dockerfile`
+ `docker-compose.yml` work for any container host.
