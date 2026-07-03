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
        conn.commit()


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
