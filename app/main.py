"""Recoup FastAPI app — Day 1 skeleton.

Exposes a health check (app + database) and a debug endpoint that round-trips a
prompt through the provider-agnostic LLM client, so the whole Day 1 stack —
provider abstraction, fallback, and cost logging — is demo-able in a browser
before the LangGraph agent exists.
"""

import asyncio
import hashlib
import hmac
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app import db, notify
from app.auth import rate_limit, require_api_key
from app.config import get_settings
from app.context import request_id_var
from graph import run_turn
from graph.actions import Action
from llm import LLMError, get_client

logging.basicConfig(level=logging.INFO)

_STATIC_DIR = Path(__file__).parent / "static"

# Shared process-wide client — the same instance the graph nodes use, so the
# cost report reflects calls made inside the graph.
llm_client = get_client()


async def _retention_loop(days: int) -> None:
    """Daily data-retention purge (#12)."""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            result = await asyncio.to_thread(db.purge_old_data, days)
            logging.info("retention purge: %s", result)
        except Exception as exc:
            logging.warning("retention purge failed: %s", exc)


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
    """Startup/shutdown: error tracking, DB schema, retention job, pool teardown."""
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
        if settings.data_retention_days > 0:
            tasks.append(asyncio.create_task(_retention_loop(settings.data_retention_days)))
    else:
        logging.info("DATABASE_URL not set — running without persistence/RAG store "
                     "(memory off, retrieval uses built-in strategies)")
    yield
    for t in tasks:
        t.cancel()
    db.close_pool()


app = FastAPI(title="Recoup", version="0.1.0", lifespan=lifespan)


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
    return response


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


class PingRequest(BaseModel):
    message: str = "Reply with a single short sentence confirming you are online."
    conversation_id: str = "demo"


@app.post("/llm/ping")
def llm_ping(req: PingRequest) -> dict:
    """Round-trip a prompt through the LLM client to prove providers + fallback work."""
    settings = get_settings()
    try:
        result = llm_client.complete(
            system="You are Recoup's connectivity check. Keep replies to one sentence.",
            messages=[{"role": "user", "content": req.message}],
            conversation_id=req.conversation_id,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

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


@app.get("/llm/cost")
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


class ChatRequest(BaseModel):
    conversation_id: str
    message: str = ""               # empty = initial failed-payment event
    # Context for a new conversation (ignored if the conversation already exists).
    customer_name: str = "there"
    decline_code: DeclineCode = "insufficient_funds"
    processor: Processor = "paystack"
    language: Language = "english"
    amount: float = 0.0
    currency: str = "NGN"


def _payment_link(conversation_id: str) -> str:
    """Demo payment link. In production this is a real Paystack/Flutterwave URL."""
    return f"https://pay.recoup.africa/{conversation_id}"


# Bounded worker pool so a turn can be given a hard deadline (#8).
_TURN_EXECUTOR = ThreadPoolExecutor(max_workers=8)


@app.post("/chat", dependencies=[Depends(rate_limit)])
def chat(req: ChatRequest) -> dict:
    """Run one turn of the recovery graph, persisting memory across turns."""
    # Serialise concurrent turns for this conversation so memory isn't clobbered (#3).
    with db.conversation_lock(req.conversation_id) as acquired:
        if not acquired:
            raise HTTPException(status_code=409,
                                detail="a message for this conversation is already being processed")
        # #8 hard deadline on the turn so a hung provider can't pin the request.
        future = _TURN_EXECUTOR.submit(_run_chat, req)
        try:
            return future.result(timeout=get_settings().request_timeout_seconds)
        except FuturesTimeout:
            raise HTTPException(status_code=504, detail="recovery turn timed out")


def _run_chat(req: "ChatRequest") -> dict:
    prior = None
    try:
        prior = db.load_conversation(req.conversation_id)
    except Exception as exc:
        logging.warning("chat: could not load memory (continuing fresh): %s", exc)

    if prior is None:
        # New conversation: seed context from the request.
        state = {
            "conversation_id": req.conversation_id,
            "customer_name": req.customer_name,
            "decline_code": req.decline_code,
            "processor": req.processor,
            "language": req.language,
            "amount": req.amount,
            "currency": req.currency,
            "history": [],
            "promises": [],
        }
    else:
        state = prior

    state["customer_message"] = req.message

    # Safety (#21): stop spending on a runaway conversation. Use persisted spend
    # (global across workers) when a DB is present, else the in-process report (#2).
    settings = get_settings()
    spent = (db.conversation_cost(req.conversation_id) if db.enabled()
             else llm_client.cost_report(req.conversation_id).get("total_cost_usd", 0.0))
    if spent >= settings.max_cost_per_conversation:
        logging.warning("conversation %s hit spend cap ($%.4f)", req.conversation_id, spent)
        _handoff(req.conversation_id, state.get("customer_name", "there"),
                 "spend_cap", req.message)
        return {
            "reply": "Thanks — let me bring in a teammate to help you finish this up.",
            "route": "needs_human",
            "action": {"type": Action.ESCALATE_TO_HUMAN},
            "payment_link": None, "promises": state.get("promises", []),
            "strategy_sources": [], "cost": llm_client.cost_report(req.conversation_id),
        }

    try:
        result = run_turn(state)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        # Surface the real cause (and full traceback in the server log) instead of
        # a generic 500, so failures are debuggable from the browser.
        logging.exception("chat: graph run failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    try:
        db.save_conversation(req.conversation_id, result)
    except Exception as exc:
        logging.warning("chat: could not persist memory: %s", exc)

    action = result.get("action") or {}
    # #18 record a human handoff (+ optional email) whenever we escalate.
    if action.get("type") == Action.ESCALATE_TO_HUMAN:
        _handoff(req.conversation_id, result.get("customer_name", "there"),
                 result.get("route", "escalation"), req.message)

    payment_link = (
        _payment_link(req.conversation_id)
        if action.get("type") == "SEND_PAYMENT_LINK"
        else None
    )
    return {
        "reply": result.get("reply", ""),
        "route": result.get("route"),
        "action": action,
        "payment_link": payment_link,
        "promises": result.get("promises", []),
        "strategy_sources": result.get("strategy_sources", []),
        "cost": llm_client.cost_report(req.conversation_id),
    }


def _handoff(conversation_id: str, customer_name: str, reason: str, message: str) -> None:
    """Record a human handoff to the DB and try to email it (best-effort)."""
    try:
        db.record_handoff(conversation_id, customer_name, reason, message)
    except Exception as exc:
        logging.warning("could not record handoff: %s", exc)
    notify.send_handoff_email(conversation_id, customer_name, reason, message)


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
    conversation_id: str
    outcome: Outcome
    amount: float = 0.0
    currency: str = "NGN"


@app.post("/outcome", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def record_outcome(req: OutcomeRequest) -> dict:
    """Feedback loop (#24): record how a conversation ended."""
    db.record_outcome(req.conversation_id, req.outcome, req.amount, req.currency)
    return {"ok": True, "stats": db.outcome_stats()}


class LearnedStrategy(BaseModel):
    content: str
    decline_code: str | None = None
    processor: str | None = None


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


class PaymentEvent(BaseModel):
    event_id: str            # processor's unique event id (for idempotency)
    conversation_id: str
    customer_name: str = "there"
    decline_code: DeclineCode = "insufficient_funds"
    processor: Processor = "paystack"
    amount: float = 0.0
    currency: str = "NGN"


@app.post("/events/payment-failed", dependencies=[Depends(rate_limit)])
async def payment_failed(request: Request) -> dict:
    """Idempotent, signature-verified failed-payment webhook (#23, #9).

    In production this is called by Paystack/Flutterwave. When WEBHOOK_SECRET is set,
    the request body is HMAC-SHA512 verified (Paystack-style). Duplicate deliveries
    (same event_id) are ignored so a retry can't start two recovery threads.
    """
    body = await request.body()
    secret = get_settings().webhook_secret
    if secret:
        signature = (request.headers.get("x-signature")
                     or request.headers.get("x-paystack-signature") or "")
        expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        evt = PaymentEvent.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid event payload: {exc}") from exc

    if db.already_processed(evt.event_id):
        return {"ok": True, "duplicate": True}
    db.mark_processed(evt.event_id)
    return {"ok": True, "duplicate": False, "conversation_id": evt.conversation_id}
