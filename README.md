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

# Provider abstraction smoke test + per-conversation cost report
curl -X POST http://localhost:8000/llm/ping -H 'content-type: application/json' -d '{"message":"hi"}'
curl http://localhost:8000/llm/cost
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

| Metric | v1 | current |
| --- | --- | --- |
| **Routing accuracy** | 96% | **100%** |
| **Action correctness** | **86%** | **100%** |
| Tone (LLM judge) | 0.86 | 0.86 |
| Language pass rate (Pidgin) | 100% | 100% |
| Avg cost / conversation | $0.0103 | $0.0087 |

Run on `claude-opus-4-8`, 28 scenarios. The v1 baseline is preserved at
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
| `DATABASE_URL` | Postgres (host `db` inside compose) |

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
| Reliability | Connection pool, request-id middleware, idempotent inbound webhook, graceful errors | `app/db.py`, `app/main.py` |
| Safety | Prompt-injection guard → human handoff; per-conversation spend cap | `graph/safety.py`, `app/main.py` |
| Human handoff | Every escalation recorded to a `handoffs` queue (`GET /handoffs`); SMTP email when configured | `app/notify.py`, `app/db.py` |
| Observability | Per-call metadata persisted (`llm_calls`); `GET /metrics` aggregates cost/latency/provider | `app/db.py` |
| Feedback loop | Record conversation outcomes (`POST /outcome`); fold winning strategies back into RAG (`POST /playbook`) | `app/main.py` |
| Analytics | Dashboard at `/dashboard` (recovery rate, revenue recovered, cost, funnel) | `app/static/dashboard.html` |
| Localisation | English, Nigerian Pidgin, Spanish, French, Swahili | `graph/prompts.py` |
| PII/compliance | Generated text kept out of logs; data-retention purge; posture in [COMPLIANCE.md](COMPLIANCE.md) | `llm/client.py`, `app/db.py` |
| Testing/CI | 21 pytest tests run in GitHub Actions | `tests/`, `.github/workflows/ci.yml` |

## Known limitations / what I'd do for production

- **Synthetic data, demo mode.** The failed-payment webhook and payment link are
  scaffolded but not wired to live Paystack/Flutterwave; WhatsApp is a browser
  stand-in for the Business API.
- **Retry engine.** `SCHEDULE_RETRY` records intent; a production system needs a job
  queue to actually re-attempt charges at payday-timed moments.
- **Vector DB scaling.** pgvector is right for this size; at scale I'd add an IVFFlat/
  HNSW index and consider a dedicated vector DB.
- **Auth/multi-tenancy.** Endpoints are open for the demo; production needs API-key
  auth, rate limiting, and per-merchant isolation.
- **Eval coverage.** 28 scenarios is a credible start; production would grow the set
  from real (anonymised) conversations with regression gating in CI.

## Deploying (Render blueprint)

A [`render.yaml`](render.yaml) blueprint provisions the web service **and** a free
Postgres (with `pgvector`):

1. Push this repo to GitHub.
2. In Render: **New + → Blueprint**, select the repo. It reads `render.yaml`.
3. Set `ANTHROPIC_API_KEY` (and optionally `OPENAI_API_KEY`) in the dashboard —
   they're marked `sync:false` so they're never committed.
4. After the first deploy, open the service **Shell** and run once:
   `python -m rag.ingest` to load the knowledge base.
5. Health check: `GET /health`. Demo: `/`. Dashboard: `/dashboard`.

A `Procfile` is included for Railway/Fly/Heroku-style platforms; the `Dockerfile`
+ `docker-compose.yml` work for any container host.
