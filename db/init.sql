-- Runs once when the Postgres volume is first initialised.
-- The app also creates these idempotently on startup (see app/db.py:init_schema),
-- so the stack is correct even against a pre-existing volume.

CREATE EXTENSION IF NOT EXISTS vector;

-- Persistent conversation memory: graph state carried across turns.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    state           JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RAG store for the Decline-Intelligence node (populated by `python -m rag.ingest`).
-- 1536 dims matches OpenAI text-embedding-3-small.
CREATE TABLE IF NOT EXISTS playbook_chunks (
    id           SERIAL PRIMARY KEY,
    content      TEXT NOT NULL,
    decline_code TEXT,
    processor    TEXT,
    source       TEXT,
    embedding    vector(1536)
);
