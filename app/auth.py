"""API-key auth and a lightweight rate limiter (#1).

`require_api_key` protects write/admin endpoints. It fails closed: the caller must
send an `X-API-Key` header matching RECOUP_API_KEY, and when that setting is unset
the endpoints are disabled outright rather than left open.

The rate limiter is a per-process fixed-window counter keyed by client IP — enough
to blunt abuse in the demo. A production multi-instance deploy would back this with
Redis; that limitation is called out in the README.
"""

from __future__ import annotations

import hmac
import threading
import time

from fastapi import Header, HTTPException, Request

from app.config import get_settings

_WINDOW_SECONDS = 60
_MAX_TRACKED_CLIENTS = 10_000  # prune stale windows past this so memory stays bounded
_hits: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Dependency: enforce the API key on protected endpoints (fail-closed)."""
    configured = get_settings().recoup_api_key
    if not configured:
        raise HTTPException(status_code=503,
                            detail="endpoint disabled: RECOUP_API_KEY is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), configured.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def client_ip(request: Request) -> str:
    """Client IP, honouring the proxy-appended X-Forwarded-For entry when trusted.

    The right-most entry is the one our proxy added; anything to its left is
    client-supplied and spoofable, so it's ignored.
    """
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request) -> None:
    """Dependency: fixed-window per-IP rate limit."""
    limit = get_settings().rate_limit_per_minute
    if limit <= 0:
        return
    client = client_ip(request)
    now = time.time()
    with _lock:
        if len(_hits) > _MAX_TRACKED_CLIENTS:
            for key in [k for k, (_, start) in _hits.items() if now - start >= _WINDOW_SECONDS]:
                del _hits[key]
        count, window_start = _hits.get(client, (0, now))
        if now - window_start >= _WINDOW_SECONDS:
            count, window_start = 0, now
        count += 1
        _hits[client] = (count, window_start)
    if count > limit:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
