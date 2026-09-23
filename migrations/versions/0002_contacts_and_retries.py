"""contacts and scheduled retries

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23

Adds the WhatsApp phone → conversation map and the retry queue behind
SCHEDULE_RETRY. Idempotent, like 0001, so it applies cleanly to a database that
app.db.init_schema already bootstrapped.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS contacts (
        phone TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS scheduled_retries (
        id SERIAL PRIMARY KEY, conversation_id TEXT NOT NULL,
        due_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_retries_due ON scheduled_retries (due_at) "
    "WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_retries_conv ON scheduled_retries (conversation_id)",
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scheduled_retries CASCADE")
    op.execute("DROP TABLE IF EXISTS contacts CASCADE")
