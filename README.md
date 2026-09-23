# Recoup

A conversational, WhatsApp-first failed-payment recovery engine for African
subscription businesses on Paystack and Flutterwave. When a recurring charge
fails, an AI agent runs a personalised recovery conversation — adapting tone,
language (including Pidgin) and timing — and closes the payment in-thread with a
link, a scheduled retry, or a handoff to a human.

It runs in **demo mode on synthetic data** out of the box: no Paystack,
Flutterwave or WhatsApp credentials are needed to try it. Each integration switches
on when its keys are added ([Going live](docs/going-live.md)).

## What's here

| Layer | Where | What it does |
| --- | --- | --- |
| Multi-agent graph | [graph/](graph/) | LangGraph: Router → Decline-Intelligence (RAG) → Negotiator → Memory, with policy routing and a deterministic escalation branch |
| RAG | [rag/](rag/) | Decline-code knowledge base and playbook in pgvector; metadata-filtered vector retrieval |
| LLM access | [llm/client.py](llm/client.py) | One interface over Anthropic and OpenAI, with fallback routing and per-call token/cost logging |
| Recovery lifecycle | [app/](app/) | FastAPI app: processor webhooks, WhatsApp channel, retry queue, outcomes, browser demo and dashboard |
| Eval harness | [eval/](eval/) | 28 scenarios scored on routing, action, latency, cost and tone (LLM-as-judge) |

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
                 needs_human / dispute ┌─────────┴───────────────┐ else
                                       ▼                         ▼
                               ┌──────────────┐         ┌─────────────────────┐
                               │  Escalate    │         │ Decline-Intelligence│  RAG: strategy for this
                               │(deterministic)│        │       (RAG)         │  decline code + processor
                               └──────┬───────┘         └──────────┬──────────┘
                                      │                            ▼
                                      │                  ┌──────────────────┐
                                      │                  │   Negotiator     │  reply + structured
                                      │                  │   (responder)    │  action JSON
                                      │                  └────────┬─────────┘
                                      └───────────┬───────────────┘
                                                  ▼
                                           ┌──────────────┐
                                           │   Memory     │  history + promises,
                                           └──────┬───────┘  persisted to Postgres
                                                  ▼
                            reply + action (SEND_PAYMENT_LINK / SCHEDULE_RETRY / ESCALATE_TO_HUMAN)
```

Around the graph, the app turns those actions into real effects: checkout links,
a retry queue that re-charges or reminds on the agreed day, handoffs, and outcomes
that close when the processor reports the payment succeeded.

## Quick start

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY and/or OPENAI_API_KEY
docker compose up --build
```

- **Demo** (simulated WhatsApp thread): <http://localhost:8000/>
- **Dashboard:** <http://localhost:8000/dashboard>
- **API docs:** <http://localhost:8000/docs>

The knowledge base loads automatically on first start. The demo opens as if a
processor webhook just reported a failed charge; reply as the customer, and use the
payment link to see the conversation marked recovered.

## Eval results

```bash
docker compose exec app python -m eval.runner            # with the tone judge
docker compose exec app python -m eval.runner --no-judge # faster, cheaper
```

| Metric | v1 | v2 | current |
| --- | --- | --- | --- |
| **Routing accuracy** | 96% | **100%** | **100%** |
| **Action correctness** | **86%** | **100%** | **100%** |
| Tone (LLM judge) | 0.86 | 0.86 | 0.88 |
| Language pass rate (Pidgin) | 100% | 100% | 100% |
| Avg cost / conversation | $0.0103 | $0.0087 | $0.0115 |

What moved the numbers, each found by reading the failing scenarios:

- **v1 → v2:** disputes sometimes got a payment link instead of a human → disputes
  now route to the deterministic Escalate node. A plain "hello" was escalated to a
  human → greetings continue the conversation, and affirmatives route as `pay_later`.
- **v2 → current:** scheduled retries and processor triggers lengthened the Negotiator
  prompt (hence the cost). The rerun caught "later" misrouting and a flat reply when a
  customer came back as promised; both were fixed in the prompts.

28 scenarios on `claude-opus-4-8`. Scorecards: [current](eval/scorecard.md),
[v1 baseline](eval/scorecard_v1.md). A nightly CI job reruns the eval as a
regression gate.

## Engineering decisions

- **A graph, not one big prompt.** Classification, retrieval, response generation
  and memory are separate, independently testable nodes. Routing is explicit and
  auditable — disputes and escalations bypass the Negotiator with a fixed reply — which is also what lets
  the eval harness pinpoint which step failed.
- **Chunking by heading.** Each decline code's guidance is a self-contained unit, so
  the knowledge base is chunked one section per `##` heading, with decline code and
  processor parsed into metadata so retrieval filters before it ranks. Embeddings use
  `text-embedding-3-small`: cheap, fast, and ample for a few dozen short chunks.
- **Degrades at every level.** No embeddings key → metadata-only ranking; empty store
  → built-in strategies; no processor keys → simulated checkout; no WhatsApp → browser
  thread. The demo works with just one LLM key.
- **Provider-agnostic with fallback.** Structured actions use prompt-based JSON rather
  than each provider's native format, so the fallback path behaves identically.
- **Cost as a first-class metric.** Every call is priced and logged, per-conversation
  and daily spend are capped, and the eval reports cost next to accuracy.

## Known limitations

- **Integrations are verified against documented payloads, not live accounts.** The
  Paystack, Flutterwave and WhatsApp adapters are tested with synthetic data; exercising
  them against real merchant accounts is the next step before real customers.
- **Retry timing is simple.** Retries fire at 08:00 UTC on the day the customer named.
  Production would learn payday patterns and respect per-timezone quiet hours.
- **Only Paystack re-charges saved cards;** Flutterwave retries send a reminder.
- **No multi-tenancy** — no per-merchant isolation, onboarding or billing yet.
- **Eval coverage** is 28 scenarios; production would grow it from real, anonymised
  conversations.

## Deploying

A [`render.yaml`](render.yaml) blueprint provisions the web service and a Postgres
database with pgvector. In Render, choose **New + → Blueprint**, select the repo, and
set `ANTHROPIC_API_KEY` (optionally `OPENAI_API_KEY`). An admin API key is generated
automatically, and public LLM spend is capped per day. A `Dockerfile` and `Procfile`
cover other hosts.

## More docs

- [Configuration](docs/configuration.md) — every environment variable
- [Going live](docs/going-live.md) — connecting Paystack, Flutterwave and WhatsApp
- [Production features & observability](docs/production.md)
- [Compliance & data handling](COMPLIANCE.md)
