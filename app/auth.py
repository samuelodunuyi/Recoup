"""API-key auth and a lightweight rate limiter (#1).

`require_api_key` protects write/admin endpoints: when RECOUP_API_KEY is set, the
caller must send a matching `X-API-Key` header. When it's unset (demo), it's open.

The rate limiter is a per-process fixed-window counter keyed by client IP — enough
to blunt abuse in the demo. A production multi-instance deploy would back this with
Redis; that limitation is called out in the README.
"""

from __future__ import annotations

import threading
import time

from fastapi import Header, HTTPException, Request

from app.config import get_settings

_WINDOW_SECONDS = 60
_hits: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Dependency: enforce the API key on protected endpoints when configured."""
    configured = get_settings().recoup_api_key
    if configured and x_api_key != configured:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def rate_limit(request: Request) -> None:
    """Dependency: fixed-window per-IP rate limit."""
    limit = get_settings().rate_limit_per_minute
    if limit <= 0:
        return
    client = request.client.host if request.client else "unknown"
    now = time.time()
    with _lock:
        count, window_start = _hits.get(client, (0, now))
        if now - window_start >= _WINDOW_SECONDS:
            count, window_start = 0, now
        count += 1
        _hits[client] = (count, window_start)
    if count > limit:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
