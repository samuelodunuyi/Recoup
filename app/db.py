"""Postgres helpers: health check, schema bootstrap, and conversation memory.

The RAG store (`playbook_chunks`) lives here too but is read/written by `rag/`.
"""

import json

import psycopg

from app.config import get_settings


def enabled() -> bool:
    """Whether a database is configured. Empty DATABASE_URL = run without one
    (no persistence, RAG falls back to built-in strategies)."""
    return bool(get_settings().database_url.strip())


def connect() -> psycopg.Connection:
    # prepare_threshold=None disables server-side prepared statements, which keeps
    # us compatible with connection poolers like Supabase's (PgBouncer).
    return psycopg.connect(
        get_settings().database_url, connect_timeout=5, prepare_threshold=None
    )


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
