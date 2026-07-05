"""Postgres helpers: health check, schema bootstrap, and conversation memory.

The RAG store (`playbook_chunks`) lives here too but is read/written by `rag/`.
"""

import json
import logging

from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from app.config import get_settings

logger = logging.getLogger("recoup.db")

_pool: ConnectionPool | None = None


def enabled() -> bool:
    """Whether a database is configured. Empty DATABASE_URL = run without one
    (no persistence, RAG falls back to built-in strategies)."""
    return bool(get_settings().database_url.strip())


def _configure(conn) -> None:
    # Register the pgvector adapters on each pooled connection. The extension may
    # not exist yet on a brand-new DB (init_schema creates it) — ignore if so.
    try:
        register_vector(conn)
    except Exception as exc:
        logger.debug("register_vector skipped (extension not ready?): %s", exc)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # min_size=0 so a briefly-unavailable DB doesn't block process startup;
        # prepare_threshold=None keeps us compatible with poolers (Supabase/PgBouncer).
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=0,
            max_size=10,
            kwargs={"prepare_threshold": None, "connect_timeout": 10},
            configure=_configure,
            open=True,
        )
    return _pool


def connect():
    """Context manager yielding a pooled connection (returned to the pool on exit)."""
    return _get_pool().connection()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ping() -> bool:
    """Return True if the database answers `SELECT 1`."""
    if not enabled():
        return False
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        return cur.fetchone() == (1,)


def init_schema() -> None:
    """Create the extension and tables idempotently (safe on every startup)."""
    if not enabled():
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                state           JSONB NOT NULL,
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS playbook_chunks (
                id           SERIAL PRIMARY KEY,
                content      TEXT NOT NULL,
                decline_code TEXT,
                processor    TEXT,
                source       TEXT,
                embedding    vector(1536)
            )
            """
        )
        # Human-handoff queue (#18): every escalation is recorded here.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS handoffs (
                id              SERIAL PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                customer_name   TEXT,
                reason          TEXT,
                last_message    TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Observability (#11): one row per LLM call (metadata only, no message text).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
                id                SERIAL PRIMARY KEY,
                conversation_id   TEXT,
                provider          TEXT,
                model             TEXT,
                prompt_tokens     INT,
                completion_tokens INT,
                latency_ms        REAL,
                cost_usd          DOUBLE PRECISION,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()


def record_handoff(conversation_id: str, customer_name: str, reason: str,
                   last_message: str) -> None:
    if not enabled():
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO handoffs (conversation_id, customer_name, reason, last_message)
               VALUES (%s, %s, %s, %s)""",
            (conversation_id, customer_name, reason, last_message),
        )
        conn.commit()


def list_handoffs(limit: int = 50) -> list[dict]:
    if not enabled():
        return []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT conversation_id, customer_name, reason, last_message, created_at
               FROM handoffs ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        cols = ("conversation_id", "customer_name", "reason", "last_message", "created_at")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def record_llm_call(rec: dict) -> None:
    """Persist one LLM call's metadata (wired as the client's cost sink)."""
    if not enabled():
        return
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO llm_calls
                   (conversation_id, provider, model, prompt_tokens, completion_tokens,
                    latency_ms, cost_usd)
                   VALUES (%(conversation_id)s, %(provider)s, %(model)s, %(prompt_tokens)s,
                           %(completion_tokens)s, %(latency_ms)s, %(cost_usd)s)""",
                rec,
            )
            conn.commit()
    except Exception as exc:  # observability must never break the request path
        logger.warning("record_llm_call failed: %s", exc)


def metrics() -> dict:
    """Aggregate observability metrics from persisted LLM calls."""
    if not enabled():
        return {"enabled": False}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*), coalesce(sum(cost_usd),0), coalesce(avg(latency_ms),0),
                      count(DISTINCT conversation_id)
               FROM llm_calls"""
        )
        calls, cost, avg_latency, convos = cur.fetchone()
        cur.execute(
            "SELECT provider, count(*), coalesce(sum(cost_usd),0) FROM llm_calls GROUP BY provider"
        )
        by_provider = {p: {"calls": c, "cost_usd": round(float(s), 6)} for p, c, s in cur.fetchall()}
        cur.execute("SELECT count(*) FROM handoffs")
        handoffs = cur.fetchone()[0]
    return {
        "enabled": True,
        "llm_calls": calls,
        "conversations": convos,
        "total_cost_usd": round(float(cost), 6),
        "avg_latency_ms": round(float(avg_latency), 1),
        "by_provider": by_provider,
        "handoffs": handoffs,
    }


def load_conversation(conversation_id: str) -> dict | None:
    """Return the persisted graph state for a conversation, or None if new."""
    if not enabled():
        return None
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM conversations WHERE conversation_id = %s",
            (conversation_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def save_conversation(conversation_id: str, state: dict) -> None:
    """Upsert the graph state so memory survives across turns."""
    if not enabled():
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversations (conversation_id, state, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (conversation_id)
            DO UPDATE SET state = EXCLUDED.state, updated_at = now()
            """,
            (conversation_id, json.dumps(state)),
        )
        conn.commit()
