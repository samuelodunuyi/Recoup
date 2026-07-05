"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-05

Idempotent raw DDL (CREATE ... IF NOT EXISTS) so it can be applied to a database
that was previously bootstrapped by app.db.init_schema without conflicting. This is
the single canonical schema for production; init_schema remains for quick local use.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        state           JSONB NOT NULL,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS playbook_chunks (
        id SERIAL PRIMARY KEY, content TEXT NOT NULL, decline_code TEXT,
        processor TEXT, source TEXT, embedding vector(1536))""",
    """CREATE TABLE IF NOT EXISTS handoffs (
        id SERIAL PRIMARY KEY, conversation_id TEXT NOT NULL, customer_name TEXT,
        reason TEXT, last_message TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS llm_calls (
        id SERIAL PRIMARY KEY, conversation_id TEXT, provider TEXT, model TEXT,
        prompt_tokens INT, completion_tokens INT, latency_ms REAL,
        cost_usd DOUBLE PRECISION, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS outcomes (
        conversation_id TEXT PRIMARY KEY, outcome TEXT NOT NULL,
        amount DOUBLE PRECISION DEFAULT 0, currency TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS processed_events (
        event_id TEXT PRIMARY KEY, processed_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_conv ON llm_calls (conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_created ON handoffs (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON outcomes (outcome)",
    "CREATE INDEX IF NOT EXISTS idx_playbook_embedding ON playbook_chunks "
    "USING hnsw (embedding vector_cosine_ops)",
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in ("processed_events", "outcomes", "llm_calls", "handoffs",
                  "playbook_chunks", "conversations"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
