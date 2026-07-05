"""Request-scoped context (#13).

A contextvar holding the current request id, set by the API middleware and read by
the LLM client and graph tracer so every log line for a request can be correlated.
"""

from __future__ import annotations

from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
