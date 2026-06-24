"""pgvector-backed store for the Decline-Intelligence node.

Retrieval is hybrid: a metadata filter (decline code + processor) narrows to the
relevant rows, then vector similarity ranks them. When embeddings are unavailable
(no OpenAI key), it falls back to a metadata-only ranking so the node still works.
"""

from __future__ import annotations

import logging

from pgvector.psycopg import register_vector

from app import db
from rag.embeddings import embed_text

logger = logging.getLogger("recoup.rag")


def _conn():
    conn = db.connect()
    register_vector(conn)
    return conn


def count() -> int:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM playbook_chunks")
        return cur.fetchone()[0]


def clear() -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE playbook_chunks RESTART IDENTITY")
        conn.commit()


def add_chunks(chunks: list[dict]) -> None:
    """Insert chunks. Each: {content, decline_code, processor, source, embedding?}."""
    with _conn() as conn, conn.cursor() as cur:
        for c in chunks:
            cur.execute(
                """
                INSERT INTO playbook_chunks (content, decline_code, processor, source, embedding)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    c["content"],
                    c.get("decline_code"),
                    c.get("processor"),
                    c.get("source"),
                    c.get("embedding"),
                ),
            )
        conn.commit()


def search(decline_code: str, processor: str, query: str, k: int = 3) -> list[dict]:
    """Return up to k strategy chunks for this decline code + processor.

    Includes general (code-less) playbook chunks alongside code-specific ones, then
    ranks by vector similarity to `query` when embeddings are present.
    """
    if not db.enabled():
        return []  # no DB configured — node falls back to built-in strategies

    query_vec = embed_text(query) if query else None

    # Relevant rows: matching decline code OR general playbook; matching processor OR
    # processor-agnostic.
    where = """
        (decline_code = %(code)s OR decline_code IS NULL)
        AND (processor IS NULL OR processor ILIKE %(proc)s)
    """
    params: dict = {
        "code": decline_code,
        "proc": f"%{processor}%" if processor else "%",
    }

    if query_vec is not None:
        order = "ORDER BY embedding <=> %(qv)s ASC NULLS LAST"
        params["qv"] = query_vec
    else:
        # No embeddings: code-specific chunks first, then by insertion order.
        order = "ORDER BY (decline_code = %(code)s) DESC NULLS LAST, id ASC"

    sql = f"""
        SELECT content, decline_code, processor, source
        FROM playbook_chunks
        WHERE {where}
        {order}
        LIMIT %(k)s
    """
    params["k"] = k

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {"content": r[0], "decline_code": r[1], "processor": r[2], "source": r[3]}
        for r in rows
    ]
