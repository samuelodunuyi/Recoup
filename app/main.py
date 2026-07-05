"""Recoup FastAPI app — Day 1 skeleton.

Exposes a health check (app + database) and a debug endpoint that round-trips a
prompt through the provider-agnostic LLM client, so the whole Day 1 stack —
provider abstraction, fallback, and cost logging — is demo-able in a browser
before the LangGraph agent exists.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import db, notify
from app.config import get_settings
from graph import run_turn
from graph.actions import Action
from llm import LLMError, get_client

logging.basicConfig(level=logging.INFO)

_STATIC_DIR = Path(__file__).parent / "static"

# Shared process-wide client — the same instance the graph nodes use, so the
# cost report reflects calls made inside the graph.
llm_client = get_client()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: bootstrap the DB schema and tear down the pool."""
    if db.enabled():
        try:
            db.init_schema()
            llm_client.set_sink(db.record_llm_call)  # #11 persist calls for observability
        except Exception as exc:  # don't block startup if DB is briefly unavailable
            logging.warning("startup: schema init failed: %s", exc)
    else:
        logging.info("DATABASE_URL not set — running without persistence/RAG store "
                     "(memory off, retrieval uses built-in strategies)")
    yield
    db.close_pool()


app = FastAPI(title="Recoup", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Reliability (#23): tag every request with an id for traceable logs."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response = await call_next(request)
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
    """Liveness for the app and its database."""
    try:
        db_ok = db.ping()
    except Exception as exc:  # surface DB connectivity without crashing the probe
        db_ok = False
        logging.warning("health: db ping failed: %s", exc)
    return {"status": "ok", "database": "ok" if db_ok else "unavailable"}


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


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    """Run one turn of the recovery graph, persisting memory across turns."""
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

    # Safety (#21): stop spending on a runaway conversation.
    settings = get_settings()
    spent = llm_client.cost_report(req.conversation_id).get("total_cost_usd", 0.0)
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


@app.get("/handoffs")
def handoffs() -> dict:
    """Recent human-handoff queue (#18)."""
    return {"handoffs": db.list_handoffs()}


@app.get("/metrics")
def metrics() -> dict:
    """Observability metrics aggregated from persisted LLM calls (#11)."""
    return db.metrics()


Outcome = Literal["recovered", "scheduled", "escalated", "lost", "pending"]


class OutcomeRequest(BaseModel):
    conversation_id: str
    outcome: Outcome
    amount: float = 0.0
    currency: str = "NGN"


@app.post("/outcome")
def record_outcome(req: OutcomeRequest) -> dict:
    """Feedback loop (#24): record how a conversation ended."""
    db.record_outcome(req.conversation_id, req.outcome, req.amount, req.currency)
    return {"ok": True, "stats": db.outcome_stats()}


class LearnedStrategy(BaseModel):
    content: str
    decline_code: str | None = None
    processor: str | None = None


@app.post("/playbook")
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


@app.get("/dashboard/data")
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


@app.post("/events/payment-failed")
def payment_failed(evt: PaymentEvent) -> dict:
    """Idempotent inbound failed-payment webhook (#23).

    In production this is called by Paystack/Flutterwave. Duplicate deliveries
    (same event_id) are ignored so a retry can't start two recovery threads.
    """
    if db.already_processed(evt.event_id):
        return {"ok": True, "duplicate": True}
    db.mark_processed(evt.event_id)
    return {"ok": True, "duplicate": False, "conversation_id": evt.conversation_id}
