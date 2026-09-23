"""Recoup FastAPI app.

HTTP surface for the recovery agent: the browser demo (`/chat`), processor and
WhatsApp webhooks, admin/observability endpoints, and a simulated checkout for the
demo. Turn orchestration lives in `app.recovery`; processor and channel specifics
live in `app.payments` and `app.whatsapp`.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import db, payments, recovery, whatsapp
from app.auth import rate_limit, require_api_key
from app.config import get_settings
from app.context import request_id_var
from app.recovery import TurnError
from llm import LLMError, get_client

logging.basicConfig(level=logging.INFO)

_STATIC_DIR = Path(__file__).parent / "static"

# Shared process-wide client — the same instance the graph nodes use, so the
# cost report reflects calls made inside the graph.
llm_client = get_client()

# Webhooks must answer the sender quickly; the recovery turn they trigger runs here.
_BACKGROUND = ThreadPoolExecutor(max_workers=4)


def _in_background(fn, *args) -> None:
    def run():
        try:
            fn(*args)
        except Exception:
            logging.exception("background task %s failed", getattr(fn, "__name__", fn))
    _BACKGROUND.submit(run)


async def _retention_loop(days: int) -> None:
    """Daily data-retention purge (#12)."""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            result = await asyncio.to_thread(db.purge_old_data, days)
            logging.info("retention purge: %s", result)
        except Exception as exc:
            logging.warning("retention purge failed: %s", exc)


async def _retry_loop(interval: int) -> None:
    """Execute SCHEDULE_RETRY actions when they come due."""
    while True:
        await asyncio.sleep(interval)
        try:
            ran = await asyncio.to_thread(recovery.run_due_retries)
            if ran:
                logging.info("scheduled retries executed: %d", ran)
        except Exception as exc:
            logging.warning("retry worker failed: %s", exc)


async def _seed_rag_if_empty() -> None:
    """One-time: load the knowledge base if the RAG store is empty, so a fresh
    deploy has semantic retrieval without a manual `python -m rag.ingest` step.
    Runs in the background; requests before it finishes fall back to built-in
    strategies."""
    try:
        from rag import store
        from rag.ingest import ingest

        if await asyncio.to_thread(store.count) == 0:
            logging.info("RAG store empty — seeding knowledge base in background...")
            n = await asyncio.to_thread(ingest)
            logging.info("RAG store seeded with %d chunks", n)
    except Exception as exc:
        logging.warning("RAG auto-seed skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: error tracking, DB schema, background jobs, pool teardown."""
    settings = get_settings()
    if settings.sentry_dsn:  # #13 optional error tracking
        try:
            import sentry_sdk
            sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)
            logging.info("Sentry error tracking enabled")
        except Exception as exc:
            logging.warning("Sentry init failed: %s", exc)

    tasks = []
    if db.enabled():
        try:
            db.init_schema()
            llm_client.set_sink(db.record_llm_call)  # #11 persist calls for observability
        except Exception as exc:  # don't block startup if DB is briefly unavailable
            logging.warning("startup: schema init failed: %s", exc)
        tasks.append(asyncio.create_task(_seed_rag_if_empty()))  # auto-seed on first deploy
        tasks.append(asyncio.create_task(_retry_loop(max(10, settings.retry_poll_seconds))))
        if settings.data_retention_days > 0:
            tasks.append(asyncio.create_task(_retention_loop(settings.data_retention_days)))
    else:
        logging.info("DATABASE_URL not set — running without persistence/RAG store "
                     "(memory off, retrieval uses built-in strategies, no retries)")
    yield
    for t in tasks:
        t.cancel()
    db.close_pool()


app = FastAPI(title="Recoup", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Reliability (#23/#13): tag every request with an id that flows into logs."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["x-request-id"] = request_id
    # Baseline hardening headers for the demo pages and API responses.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _turn_or_http(fn, *args, **kwargs) -> dict:
    """Map recovery TurnErrors onto HTTP responses."""
    try:
        return fn(*args, **kwargs)
    except TurnError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@app.get("/")
def demo_ui() -> FileResponse:
    """Serve the fake-WhatsApp demo thread."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/dashboard")
def dashboard_ui() -> FileResponse:
    """Analytics dashboard (#20)."""
    return FileResponse(_STATIC_DIR / "dashboard.html")


@app.get("/health")
def health() -> dict:
    """Liveness — the process is up (does not depend on the DB)."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness (#14): DB reachable and at least one provider key configured."""
    s = get_settings()
    try:
        db_ok = db.ping() if db.enabled() else True
    except Exception:
        db_ok = False
    provider_ok = bool(s.anthropic_api_key or s.openai_api_key)
    is_ready = db_ok and provider_ok
    body = {"ready": is_ready, "database": "ok" if db_ok else "unavailable",
            "providers": "ok" if provider_ok else "unconfigured"}
    return JSONResponse(body, status_code=200 if is_ready else 503)


# Conversation ids are client-chosen, so constrain them: bounded length and a safe
# charset (they appear in URLs, logs, and advisory-lock keys).
ConversationId = Annotated[str, Field(min_length=1, max_length=64,
                                      pattern=r"^[A-Za-z0-9_-]+$")]


class PingRequest(BaseModel):
    message: str = Field("Reply with a single short sentence confirming you are online.",
                         max_length=500)
    conversation_id: ConversationId = "ping"


@app.post("/llm/ping", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def llm_ping(req: PingRequest) -> dict:
    """Round-trip a prompt through the LLM client to prove providers + fallback work."""
    settings = get_settings()
    if recovery.daily_budget_exceeded():
        raise HTTPException(status_code=503,
                            detail="daily usage limit reached, please try again later")
    try:
        result = llm_client.complete(
            system="You are Recoup's connectivity check. Keep replies to one sentence.",
            messages=[{"role": "user", "content": req.message}],
            conversation_id=req.conversation_id,
            max_tokens=100,
        )
    except LLMError as exc:
        logging.warning("llm ping failed: %s", exc)
        raise HTTPException(status_code=502, detail="LLM providers unavailable") from exc

    return {
        "reply": result.text,
        "served_by": {"provider": result.provider, "model": result.model},
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "latency_ms": round(result.latency_ms, 1),
            "cost_usd": result.cost_usd,
        },
        "primary_provider": settings.llm_primary,
    }


@app.get("/llm/cost", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def llm_cost() -> dict:
    """Per-conversation, per-provider cost report accumulated this process."""
    return llm_client.cost_report()


# Validated enums — invalid values now return a clear 422 instead of odd behaviour.
DeclineCode = Literal[
    "insufficient_funds", "card_declined", "expired_card",
    "do_not_honour", "transaction_limit", "mobile_money_timeout",
]
Processor = Literal["paystack", "flutterwave", "mobile_money"]
Language = Literal["english", "pidgin", "spanish", "french", "swahili"]


# Every free-text field below reaches the LLM prompt, so each is length-bounded
# (cost + injection surface). Names allow letters, spaces and . ' - only.
CustomerName = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[\w .'-]+$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Amount = Annotated[float, Field(ge=0, le=100_000_000)]


class ChatRequest(BaseModel):
    conversation_id: ConversationId
    message: str = Field("", max_length=1000)  # empty = initial failed-payment event
    # Context for a new conversation (ignored if the conversation already exists).
    customer_name: CustomerName = "there"
    decline_code: DeclineCode = "insufficient_funds"
    processor: Processor = "paystack"
    language: Language = "english"
    amount: Amount = 0.0
    currency: Currency = "NGN"


@app.post("/chat", dependencies=[Depends(rate_limit)])
def chat(req: ChatRequest) -> dict:
    """Run one turn of the recovery graph, persisting memory across turns."""
    context = req.model_dump(exclude={"conversation_id", "message"})
    return _turn_or_http(recovery.run_turn_locked, req.conversation_id, req.message,
                         context=context)


@app.get("/handoffs", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def handoffs() -> dict:
    """Recent human-handoff queue (#18)."""
    return {"handoffs": db.list_handoffs()}


@app.get("/metrics", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def metrics() -> dict:
    """Observability metrics aggregated from persisted LLM calls (#11)."""
    return db.metrics()


@app.get("/metrics/prometheus", dependencies=[Depends(require_api_key)])
def metrics_prometheus() -> PlainTextResponse:
    """Prometheus exposition of the same aggregates (#13, pull-based export)."""
    m = db.metrics()
    o = db.outcome_stats()
    lines = [
        "# TYPE recoup_llm_calls_total counter",
        f"recoup_llm_calls_total {m.get('llm_calls', 0)}",
        "# TYPE recoup_llm_cost_usd_total counter",
        f"recoup_llm_cost_usd_total {m.get('total_cost_usd', 0)}",
        "# TYPE recoup_avg_latency_ms gauge",
        f"recoup_avg_latency_ms {m.get('avg_latency_ms', 0)}",
        "# TYPE recoup_handoffs_total counter",
        f"recoup_handoffs_total {m.get('handoffs', 0)}",
        "# TYPE recoup_recovery_rate gauge",
        f"recoup_recovery_rate {o.get('recovery_rate', 0)}",
    ]
    for provider, stats in (m.get("by_provider") or {}).items():
        lines.append(f'recoup_llm_calls_by_provider{{provider="{provider}"}} {stats["calls"]}')
    return PlainTextResponse("\n".join(lines) + "\n")


Outcome = Literal["recovered", "scheduled", "escalated", "lost", "pending"]


class OutcomeRequest(BaseModel):
    conversation_id: ConversationId
    outcome: Outcome
    amount: Amount = 0.0
    currency: Currency = "NGN"


@app.post("/outcome", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def record_outcome(req: OutcomeRequest) -> dict:
    """Feedback loop (#24): record how a conversation ended (manual override —
    outcomes are otherwise recorded automatically from processor events)."""
    db.record_outcome(req.conversation_id, req.outcome, req.amount, req.currency)
    return {"ok": True, "stats": db.outcome_stats()}


class LearnedStrategy(BaseModel):
    # This text is fed verbatim into the Negotiator prompt, so it's admin-only
    # (API key) and bounded.
    content: str = Field(min_length=1, max_length=2000)
    decline_code: DeclineCode | None = None
    processor: Processor | None = None


@app.post("/playbook", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def add_learned_strategy(req: LearnedStrategy) -> dict:
    """Feedback loop (#24): fold a winning strategy back into the RAG playbook."""
    from rag.embeddings import embed_text
    from rag import store
    chunk = {
        "content": req.content,
        "decline_code": req.decline_code,
        "processor": req.processor,
        "source": "learned",
        "embedding": embed_text(req.content),
    }
    store.add_chunks([chunk])
    return {"ok": True, "chunks": store.count()}


@app.get("/dashboard/data", dependencies=[Depends(rate_limit)])
def dashboard_data() -> dict:
    """Aggregates behind the analytics dashboard (#20)."""
    return {"metrics": db.metrics(), "outcomes": db.outcome_stats()}


# ─── Webhooks ───────────────────────────────────────────────────────────────
# All webhooks are fail-closed (disabled until their secret is configured),
# signature-verified, size-capped, and idempotent on the sender's event id.
_MAX_WEBHOOK_BYTES = 64 * 1024


async def _read_body(request: Request) -> bytes:
    body = await request.body()
    if len(body) > _MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    return body


def _require(setting: str, name: str) -> None:
    if not setting:
        raise HTTPException(status_code=503, detail=f"webhook disabled: {name} is not configured")


def _dispatch(evt: payments.PaymentFailure | payments.PaymentSuccess | None) -> dict:
    """Route a normalised processor event: failures open a recovery conversation
    in the background; successes close one."""
    if evt is None:
        return {"ok": True, "ignored": True}
    if not db.claim_event(evt.event_id):
        return {"ok": True, "duplicate": True}
    if isinstance(evt, payments.PaymentFailure):
        _in_background(recovery.handle_payment_failed, evt)
        return {"ok": True, "duplicate": False, "conversation_id": evt.conversation_id}
    try:
        matched = recovery.handle_payment_succeeded(evt.conversation_id, evt.amount,
                                                    evt.currency)
    except Exception:
        # Release the claim so the processor's retry of this event is processed.
        db.release_event(evt.event_id)
        raise
    return {"ok": True, "duplicate": False, "recovered": matched,
            "conversation_id": evt.conversation_id}


def _verify_generic(body: bytes, request: Request) -> None:
    secret = get_settings().webhook_secret
    _require(secret, "WEBHOOK_SECRET")
    signature = (request.headers.get("x-signature")
                 or request.headers.get("x-paystack-signature") or "")
    expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid webhook signature")


class PaymentEvent(BaseModel):
    """Processor-neutral failed-payment event (for gateways without a native adapter)."""
    event_id: str = Field(min_length=1, max_length=128)  # sender's unique id (idempotency)
    conversation_id: ConversationId
    customer_name: CustomerName = "there"
    decline_code: DeclineCode = "insufficient_funds"
    processor: Processor = "paystack"
    amount: Amount = 0.0
    currency: Currency = "NGN"
    phone: str | None = Field(None, max_length=20)
    email: str | None = Field(None, max_length=254)


class PaymentSucceededEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    conversation_id: ConversationId
    amount: Amount | None = None
    currency: Currency | None = None


@app.post("/events/payment-failed", dependencies=[Depends(rate_limit)])
async def payment_failed(request: Request) -> dict:
    """Generic failed-payment webhook (HMAC-SHA512 over the body with WEBHOOK_SECRET).
    Opens the recovery conversation and sends the first message."""
    body = await _read_body(request)
    _verify_generic(body, request)
    try:
        evt = PaymentEvent.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid event payload") from exc
    return _dispatch(payments.PaymentFailure(**evt.model_dump()))


@app.post("/events/payment-succeeded", dependencies=[Depends(rate_limit)])
async def payment_succeeded(request: Request) -> dict:
    """Generic payment-succeeded webhook: marks the conversation recovered."""
    body = await _read_body(request)
    _verify_generic(body, request)
    try:
        evt = PaymentSucceededEvent.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid event payload") from exc
    return _dispatch(payments.PaymentSuccess(
        event_id=evt.event_id, conversation_id=evt.conversation_id,
        amount=evt.amount or 0.0, currency=evt.currency or ""))


def _json(body: bytes) -> dict:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="invalid event payload")
    return payload


@app.post("/webhooks/paystack")
async def paystack_webhook(request: Request) -> dict:
    """Paystack events: `invoice.payment_failed` opens a recovery conversation,
    `charge.success` closes it. Verified with the Paystack secret key."""
    _require(get_settings().paystack_secret_key, "PAYSTACK_SECRET_KEY")
    body = await _read_body(request)
    if not payments.verify_paystack(body, request.headers.get("x-paystack-signature", "")):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    return _dispatch(payments.parse_paystack(_json(body)))


@app.post("/webhooks/flutterwave")
async def flutterwave_webhook(request: Request) -> dict:
    """Flutterwave `charge.completed` events (failed → recover, successful → close)."""
    _require(get_settings().flutterwave_secret_hash, "FLUTTERWAVE_SECRET_HASH")
    body = await _read_body(request)
    if not payments.verify_flutterwave(request.headers.get("verif-hash", "")):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    return _dispatch(payments.parse_flutterwave(_json(body)))


@app.get("/webhooks/whatsapp")
def whatsapp_verify(request: Request) -> PlainTextResponse:
    """Meta's one-time subscription handshake: echo the challenge if the token matches."""
    token = get_settings().whatsapp_verify_token
    q = request.query_params
    if (token and q.get("hub.mode") == "subscribe"
            and hmac.compare_digest(q.get("hub.verify_token", "").encode(), token.encode())):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhooks/whatsapp")
async def whatsapp_inbound(request: Request) -> dict:
    """Customer replies from WhatsApp: each message runs a turn on the customer's
    conversation (found by phone number) and the reply is sent back on WhatsApp."""
    _require(get_settings().whatsapp_app_secret, "WHATSAPP_APP_SECRET")
    body = await _read_body(request)
    if not whatsapp.verify_signature(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    accepted = 0
    for msg in whatsapp.parse_inbound(_json(body)):
        if not db.claim_event(f"whatsapp:{msg.message_id}"):
            continue  # Meta redelivers until it gets a 200
        conversation_id = db.conversation_for_phone(msg.phone)
        if not conversation_id:
            logging.info("WhatsApp message from a number with no recovery conversation")
            continue
        _in_background(recovery.handle_inbound_message, conversation_id, msg.text)
        accepted += 1
    return {"ok": True, "accepted": accepted}


# ─── Demo checkout (simulated processor) ────────────────────────────────────
# Stands in for the hosted checkout when no processor key is configured, so the
# browser demo can close the loop: pay → conversation marked recovered. Limited to
# demo mode and to `demo-` conversations, so it can never touch a real one.
def _demo_conversation(conversation_id: str) -> None:
    if (not get_settings().demo_mode
            or not re.fullmatch(r"demo-[A-Za-z0-9_-]{1,59}", conversation_id)):
        raise HTTPException(status_code=404, detail="not found")


@app.get("/demo/checkout/{conversation_id}")
def demo_checkout_page(conversation_id: str) -> FileResponse:
    _demo_conversation(conversation_id)
    return FileResponse(_STATIC_DIR / "checkout.html")


@app.post("/demo/checkout/{conversation_id}", dependencies=[Depends(rate_limit)])
def demo_checkout_pay(conversation_id: str) -> dict:
    _demo_conversation(conversation_id)
    if not recovery.handle_payment_succeeded(conversation_id):
        raise HTTPException(status_code=404,
                            detail="unknown conversation (is DATABASE_URL configured?)")
    return {"ok": True, "status": "recovered"}
