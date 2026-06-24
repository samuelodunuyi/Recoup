"""Ingest the knowledge base into the pgvector store.

Chunking strategy: one chunk per `## ` heading. The decline-code KB is naturally
atomic — each code's recovery guidance is a self-contained unit — so heading-based
chunking keeps each retrievable chunk whole and on-topic rather than splitting a
strategy mid-thought. Each chunk keeps its heading for context. Metadata (decline
code + processors) is parsed from the heading so retrieval can filter before
ranking.

Run:  python -m rag.ingest
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app import db
from rag import store
from rag.embeddings import embed_documents

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("recoup.rag.ingest")

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"


def _normalise_processor(name: str) -> str:
    """Match the values the chat API sends: lowercase, spaces → underscores."""
    return name.strip().lower().replace(" ", "_")


def _parse_sections(text: str, source: str, has_codes: bool) -> list[dict]:
    """Split markdown into one chunk per `## ` heading."""
    chunks: list[dict] = []
    # Split keeping the heading; drop the file preamble before the first `## `.
    parts = re.split(r"(?m)^##\s+", text)[1:]
    for part in parts:
        lines = part.strip().splitlines()
        if not lines:
            continue
        heading = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        content = f"{heading}\n{body}".strip()

        decline_code = None
        processor = None
        if has_codes and "—" in heading:
            code_part, proc_part = heading.split("—", 1)
            decline_code = code_part.strip()
            processors = [_normalise_processor(p) for p in proc_part.split(",")]
            processor = ",".join(p for p in processors if p)

        chunks.append(
            {
                "content": content,
                "decline_code": decline_code,
                "processor": processor,
                "source": source,
            }
        )
    return chunks


def build_chunks() -> list[dict]:
    chunks: list[dict] = []
    chunks += _parse_sections(
        (KNOWLEDGE_DIR / "decline_codes.md").read_text(encoding="utf-8"),
        source="decline_codes.md",
        has_codes=True,
    )
    chunks += _parse_sections(
        (KNOWLEDGE_DIR / "playbook.md").read_text(encoding="utf-8"),
        source="playbook.md",
        has_codes=False,
    )
    return chunks


def ingest() -> int:
    db.init_schema()
    chunks = build_chunks()

    embeddings = embed_documents([c["content"] for c in chunks])
    if embeddings is not None:
        for chunk, vector in zip(chunks, embeddings):
            chunk["embedding"] = vector
    else:
        logger.warning("ingesting without embeddings — retrieval will be metadata-only")

    store.clear()
    store.add_chunks(chunks)
    total = store.count()
    logger.info("ingested %d chunks (%d with embeddings)", total,
                len(embeddings) if embeddings else 0)
    return total


if __name__ == "__main__":
    ingest()
