"""Lightweight structured tracing for graph nodes.

Emits one JSON log line per node with run id, conversation id, node name, and
duration. Combined with the LLM client's per-call token/cost logs (same
conversation id), this gives a structured trace of every agent run without a
third-party tracer. Swap in LangSmith later by setting its env vars.
"""

from __future__ import annotations

import functools
import json
import logging
import time

from app.context import request_id_var

logger = logging.getLogger("recoup.trace")


def traced(fn):
    @functools.wraps(fn)
    def wrapper(state: dict) -> dict:
        start = time.perf_counter()
        out = fn(state)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            json.dumps(
                {
                    "event": "node",
                    "node": fn.__name__,
                    "run_id": state.get("run_id"),
                    "request_id": request_id_var.get(),
                    "conversation_id": state.get("conversation_id"),
                    "duration_ms": round(duration_ms, 1),
                }
            )
        )
        return out

    return wrapper
