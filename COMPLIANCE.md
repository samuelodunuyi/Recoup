# Compliance & data handling

Recoup handles customer conversations and payment context, so it sits in scope for
payment-industry and data-protection rules. This document states the current
posture (demo) and what production requires. Naming the gaps is deliberate.

## Payments (PCI-DSS)
- **No card data is ever stored or processed by Recoup.** We never receive PANs,
  CVVs, or full card details. Retries and payment links are delegated to the
  processor (Paystack/Flutterwave) via **tokenized** references and hosted
  checkout URLs, keeping Recoup out of PCI-DSS cardholder-data scope (SAQ-A style).
- Production: verify processor webhooks with signature validation; never log card
  metadata; annual scope review.

## Data protection (Nigeria NDPR / EU GDPR)
- **Lawful basis & consent:** recovery messaging requires the merchant to have a
  lawful basis and, for WhatsApp, opt-in per Meta's policy. Consent capture is a
  production integration (not in the demo).
- **Data minimisation:** we store conversation state, promises, decline context,
  and LLM-call *metadata* only. **Generated message text is not logged** (see
  `llm/client.py`) to avoid PII in logs.
- **Retention:** `db.purge_old_data(days)` deletes conversations, LLM-call logs,
  handoffs, outcomes, and event records older than a configurable window. Wire it
  to a scheduled job in production (e.g. daily) with a defined retention period.
- **Data-subject rights:** deletion/export by `conversation_id` is straightforward
  from the `conversations`/`outcomes`/`handoffs` tables; a production system would
  expose an authenticated endpoint for access/erasure requests.
- **Data residency:** choose a DB region appropriate to the market (e.g. EU/Nigeria)
  and document sub-processors (LLM providers, hosting, WhatsApp BSP).

## PII & safety
- Customer messages are treated as potentially sensitive; they are stored for
  conversation memory but **excluded from telemetry/logs**.
- A prompt-injection guard (`graph/safety.py`) routes manipulation attempts to a
  human. A per-conversation spend cap bounds cost/abuse.
- Production additions: PII redaction at rest, model-level guardrails/moderation,
  and audit logging of human-handoff access.

## Sub-processors (production)
LLM providers (Anthropic, OpenAI), database/hosting (e.g. Supabase, Render),
and the WhatsApp Business API provider. Maintain a current sub-processor list and
DPAs.

> Status: **demo**. The controls above marked "production" are intentionally out of
> scope for the synthetic-data demo and represent the compliance roadmap.
