"""Recoup FastAPI app — Day 1 skeleton.

Exposes a health check (app + database) and a debug endpoint that round-trips a
prompt through the provider-agnostic LLM client, so the whole Day 1 stack —
provider abstraction, fallback, and cost logging — is demo-able in a browser
before the LangGraph agent exists.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import db
from app.config import get_settings
from graph import run_turn
from llm import LLMError, get_client

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Recoup", version="0.1.0")

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def demo_ui() -> FileResponse:
    """Serve the fake-WhatsApp demo thread."""
    return FileResponse(_STATIC_DIR / "index.html")

# Shared process-wide client — the same instance the graph nodes use, so the
# cost report reflects calls made inside the graph.
llm_client = get_client()


@app.on_event("startup")
def _startup() -> None:
    """Bootstrap the DB schema (idempotent) so memory + RAG tables exist."""
    if not db.enabled():
        logging.info("DATABASE_URL not set — running without persistence/RAG store "
                     "(memory off, retrieval uses built-in strategies)")
        return
    try:
        db.init_schema()
    except Exception as exc:  # don't block startup if DB is briefly unavailable
        logging.warning("startup: schema init failed: %s", exc)


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


class ChatRequest(BaseModel):
    conversation_id: str
    message: str = ""               # empty = initial failed-payment event
    # Context for a new conversation (ignored if the conversation already exists).
    customer_name: str = "there"
    decline_code: str = "insufficient_funds"
    processor: str = "paystack"
    language: str = "english"       # "english" | "pidgin"
    amount: float = 0.0
    currency: str = "NGN"


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

    return {
        "reply": result.get("reply", ""),
        "route": result.get("route"),
        "action": result.get("action"),
        "promises": result.get("promises", []),
        "strategy_sources": result.get("strategy_sources", []),
        "cost": llm_client.cost_report(req.conversation_id),
    }
