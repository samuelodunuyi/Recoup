"""Embeddings via OpenAI.

`text-embedding-3-small` (1536 dims) — chosen for a small, English/Pidgin
knowledge base: it's cheap, fast, and more than accurate enough to rank a few
dozen decline-code chunks. The dimension is pinned to the `vector(1536)` column.

If no OpenAI key is configured, the functions return None and the store falls back
to metadata-only retrieval, so the demo still works without embeddings.
"""

from __future__ import annotations

import logging

import openai

from app.config import get_settings

logger = logging.getLogger("recoup.rag")

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536


def _client() -> openai.OpenAI | None:
    key = get_settings().openai_api_key
    if not key:
        return None
    return openai.OpenAI(api_key=key)


def embed_text(text: str) -> list[float] | None:
    """Embed a single string, or None if embeddings are unavailable."""
    vectors = embed_documents([text])
    return vectors[0] if vectors else None


def embed_documents(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch, or None if no OpenAI key is configured."""
    client = _client()
    if client is None:
        logger.info("no OPENAI_API_KEY; embeddings disabled (metadata-only retrieval)")
        return None
    try:
        response = client.embeddings.create(model=EMBED_MODEL, input=texts)
    except openai.OpenAIError as exc:
        # e.g. no billing/quota — degrade to metadata-only retrieval rather than crash.
        logger.warning("embeddings call failed (%s); falling back to metadata-only", exc)
        return None
    return [item.embedding for item in response.data]
