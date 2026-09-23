"""Security regression tests: fail-closed auth, webhook verification, input bounds,
and proxy-aware client IPs. None of these reach the database or an LLM — requests
are rejected before the handler does any work."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import client_ip
from app.config import get_settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "recoup_api_key", "")
    monkeypatch.setattr(s, "webhook_secret", "")
    monkeypatch.setattr(s, "rate_limit_per_minute", 0)
    monkeypatch.setattr(s, "trust_proxy_headers", False)
    return s


@pytest.mark.parametrize("method,path", [
    ("GET", "/handoffs"), ("GET", "/metrics"), ("GET", "/metrics/prometheus"),
    ("GET", "/llm/cost"), ("POST", "/llm/ping"), ("POST", "/playbook"),
    ("POST", "/outcome"),
])
def test_admin_endpoints_disabled_without_configured_key(method, path):
    kwargs = {"json": {}} if method == "POST" else {}
    assert client.request(method, path, **kwargs).status_code == 503


def test_admin_endpoint_rejects_wrong_key(_settings):
    _settings.recoup_api_key = "correct-key"
    assert client.get("/llm/cost").status_code == 401
    assert client.get("/llm/cost", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/llm/cost", headers={"X-API-Key": "correct-key"}).status_code == 200


def test_webhook_disabled_without_secret():
    assert client.post("/events/payment-failed", content=b"{}").status_code == 503


def test_webhook_rejects_bad_signature(_settings):
    _settings.webhook_secret = "s3cret"
    r = client.post("/events/payment-failed", content=b'{"event_id":"e1"}',
                    headers={"x-paystack-signature": "deadbeef"})
    assert r.status_code == 401


def test_webhook_accepts_valid_signature_then_validates_payload(_settings):
    _settings.webhook_secret = "s3cret"
    body = b'{"event_id":"e1"}'  # signed correctly but missing conversation_id
    sig = hmac.new(b"s3cret", body, hashlib.sha512).hexdigest()
    r = client.post("/events/payment-failed", content=body,
                    headers={"x-paystack-signature": sig})
    assert r.status_code == 422
    assert "conversation_id" not in r.text  # no internal validation detail leaked


@pytest.mark.parametrize("payload", [
    {"conversation_id": "a" * 65},                              # too long
    {"conversation_id": "x\" onmouseover=alert(1) y"},          # unsafe charset
    {"conversation_id": "ok", "message": "m" * 1001},           # message too long
    {"conversation_id": "ok", "customer_name": "<script>"},     # name charset
    {"conversation_id": "ok", "customer_name": "n" * 61},       # name too long
    {"conversation_id": "ok", "currency": "naira"},             # not ISO-4217-shaped
    {"conversation_id": "ok", "amount": -5},                    # negative amount
])
def test_chat_rejects_out_of_bounds_input(payload):
    assert client.post("/chat", json=payload).status_code == 422


def _request(peer: str, xff: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_client_ip_ignores_forwarded_header_by_default():
    assert client_ip(_request("10.0.0.1", "1.2.3.4")) == "10.0.0.1"


def test_client_ip_uses_proxy_appended_hop_when_trusted(_settings):
    _settings.trust_proxy_headers = True
    # Left entries are client-supplied (spoofable); the proxy appends the real one.
    assert client_ip(_request("10.0.0.1", "6.6.6.6, 1.2.3.4")) == "1.2.3.4"
