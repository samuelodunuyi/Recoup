"""End-to-end recovery plumbing without external services: processor/WhatsApp
webhook parsing + verification, retry scheduling, payment links, and closing the
loop on a successful payment. The LLM, database and outbound HTTP are stubbed."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import db, payments, recovery, whatsapp
from app.config import get_settings
from graph.actions import Action, validate_action

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    for key, value in {
        "rate_limit_per_minute": 0, "webhook_secret": "", "paystack_secret_key": "",
        "flutterwave_secret_key": "", "flutterwave_secret_hash": "",
        "whatsapp_access_token": "", "whatsapp_phone_number_id": "",
        "whatsapp_app_secret": "", "whatsapp_verify_token": "",
        "public_base_url": "", "demo_mode": True, "max_daily_cost_usd": 0,
    }.items():
        monkeypatch.setattr(s, key, value)
    return s


@pytest.fixture
def background(monkeypatch):
    """Capture work the webhooks hand to the background pool instead of running it."""
    calls = []
    monkeypatch.setattr(main, "_in_background", lambda fn, *args: calls.append((fn, args)))
    return calls


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setattr(db, "enabled", lambda: False)


def _eid() -> str:
    return uuid.uuid4().hex[:10]


# ─── Paystack ───────────────────────────────────────────────────────────────
def _paystack_failed(customer_code="CUS_abc123", reusable=True):
    return {
        "event": "invoice.payment_failed",
        "data": {
            "invoice_code": f"INV_{_eid()}",
            "amount": 500000,  # kobo
            "authorization": {"authorization_code": "AUTH_x", "channel": "card",
                              "reusable": reusable},
            "customer": {"first_name": "Ada<script>", "email": "ada@example.com",
                         "customer_code": customer_code, "phone": "08031234567"},
            "transaction": {"currency": "NGN", "gateway_response": "Insufficient Funds"},
        },
    }


def test_parse_paystack_failure_normalises_fields():
    evt = payments.parse_paystack(_paystack_failed())
    assert isinstance(evt, payments.PaymentFailure)
    assert evt.conversation_id == "paystack-CUS_abc123"
    assert evt.amount == 5000.0 and evt.currency == "NGN"
    assert evt.decline_code == "insufficient_funds"
    assert evt.customer_name == "Adascript"  # markup characters stripped
    assert evt.authorization_code == "AUTH_x" and evt.email == "ada@example.com"


def test_parse_paystack_ignores_non_reusable_authorization():
    assert payments.parse_paystack(_paystack_failed(reusable=False)).authorization_code is None


def test_parse_paystack_success_uses_metadata_then_customer():
    with_meta = {"event": "charge.success", "data": {
        "id": 1, "amount": 250000, "currency": "NGN",
        "metadata": {"conversation_id": "demo-abc"}, "customer": {"customer_code": "CUS_z"}}}
    evt = payments.parse_paystack(with_meta)
    assert isinstance(evt, payments.PaymentSuccess)
    assert evt.conversation_id == "demo-abc" and evt.amount == 2500.0
    # Paystack sends metadata as "" when none was set.
    no_meta = {"event": "charge.success", "data": {
        "id": 2, "amount": 100, "metadata": "", "customer": {"customer_code": "CUS_z"}}}
    assert payments.parse_paystack(no_meta).conversation_id == "paystack-CUS_z"


def test_parse_paystack_ignores_other_events():
    assert payments.parse_paystack({"event": "transfer.success", "data": {}}) is None


@pytest.mark.parametrize("text,channel,code", [
    ("Insufficient Funds", "card", "insufficient_funds"),
    ("Expired Card", "card", "expired_card"),
    ("Do Not Honor", "card", "do_not_honour"),
    ("Exceeds withdrawal limit", "card", "transaction_limit"),
    ("Transaction timed out", "mobile_money", "mobile_money_timeout"),
    ("Declined", "card", "card_declined"),
    (None, "card", "insufficient_funds"),
])
def test_decline_code_mapping(text, channel, code):
    assert payments.decline_code_from_text(text, channel) == code


def test_paystack_webhook_disabled_without_key():
    assert client.post("/webhooks/paystack", content=b"{}").status_code == 503


def test_paystack_webhook_rejects_bad_signature(_settings):
    _settings.paystack_secret_key = "sk_test_x"
    r = client.post("/webhooks/paystack", content=b"{}",
                    headers={"x-paystack-signature": "nope"})
    assert r.status_code == 401


def test_paystack_failure_webhook_opens_recovery_in_background(_settings, background, no_db):
    _settings.paystack_secret_key = "sk_test_x"
    body = json.dumps(_paystack_failed()).encode()
    sig = hmac.new(b"sk_test_x", body, hashlib.sha512).hexdigest()
    r = client.post("/webhooks/paystack", content=body,
                    headers={"x-paystack-signature": sig})
    assert r.status_code == 200 and r.json()["conversation_id"] == "paystack-CUS_abc123"
    fn, (evt,) = background[0]
    assert fn is recovery.handle_payment_failed and evt.amount == 5000.0


# ─── Flutterwave ────────────────────────────────────────────────────────────
def test_parse_flutterwave_failed_and_successful():
    failed = {"event": "charge.completed", "data": {
        "id": 9, "status": "failed", "amount": 3000, "currency": "KES",
        "payment_type": "mobilemoneyke", "processor_response": "Transaction timed out",
        "tx_ref": "something-else", "customer": {"id": 42, "name": "Wanjiru Kamau",
                                                 "email": "w@example.com"}}}
    evt = payments.parse_flutterwave(failed)
    assert isinstance(evt, payments.PaymentFailure)
    assert evt.conversation_id == "flutterwave-42" and evt.processor == "mobile_money"
    assert evt.decline_code == "mobile_money_timeout" and evt.customer_name == "Wanjiru"

    ok = {"event": "charge.completed", "data": {
        "id": 10, "status": "successful", "amount": 3000, "currency": "KES",
        "tx_ref": "recoup.demo-abc.1a2b3c4d", "customer": {"id": 42}}}
    evt = payments.parse_flutterwave(ok)
    assert isinstance(evt, payments.PaymentSuccess) and evt.conversation_id == "demo-abc"


def test_flutterwave_webhook_verifies_hash(_settings, background, no_db):
    _settings.flutterwave_secret_hash = "hash123"
    assert client.post("/webhooks/flutterwave", content=b"{}",
                       headers={"verif-hash": "wrong"}).status_code == 401
    r = client.post("/webhooks/flutterwave", content=b'{"event":"transfer.completed"}',
                    headers={"verif-hash": "hash123"})
    assert r.status_code == 200 and r.json()["ignored"] is True


# ─── Generic events ─────────────────────────────────────────────────────────
def test_generic_payment_failed_starts_background_turn(_settings, background, no_db):
    _settings.webhook_secret = "s3cret"
    body = json.dumps({"event_id": _eid(), "conversation_id": "acme-1",
                       "customer_name": "Ada", "amount": 5000,
                       "phone": "+234 803 123 4567"}).encode()
    sig = hmac.new(b"s3cret", body, hashlib.sha512).hexdigest()
    r = client.post("/events/payment-failed", content=body, headers={"x-signature": sig})
    assert r.status_code == 200
    fn, (evt,) = background[0]
    assert fn is recovery.handle_payment_failed and evt.conversation_id == "acme-1"


# ─── WhatsApp ───────────────────────────────────────────────────────────────
def test_whatsapp_verify_handshake(_settings):
    _settings.whatsapp_verify_token = "tok"
    ok = client.get("/webhooks/whatsapp",
                    params={"hub.mode": "subscribe", "hub.verify_token": "tok",
                            "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhooks/whatsapp",
                     params={"hub.mode": "subscribe", "hub.verify_token": "x",
                             "hub.challenge": "1"})
    assert bad.status_code == 403


def _wa_payload(msg_id: str, text="I'll pay Friday", sender="2348031234567"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"id": msg_id, "from": sender, "type": "text", "text": {"body": text}},
        {"id": "img", "from": sender, "type": "image"},
    ]}}]}]}


def test_parse_inbound_keeps_text_only():
    msgs = whatsapp.parse_inbound(_wa_payload("wamid.1"))
    assert [(m.message_id, m.phone, m.text) for m in msgs] == \
        [("wamid.1", "2348031234567", "I'll pay Friday")]


@pytest.mark.parametrize("raw,expected", [
    ("08031234567", "2348031234567"), ("+234 803 123 4567", "2348031234567"),
    ("12", None), (None, None),
])
def test_normalize_phone(raw, expected):
    assert whatsapp.normalize_phone(raw) == expected


def test_whatsapp_inbound_routes_to_conversation(_settings, background, monkeypatch):
    _settings.whatsapp_app_secret = "appsecret"
    monkeypatch.setattr(db, "claim_event", lambda _id: True)
    monkeypatch.setattr(db, "conversation_for_phone", lambda phone: "paystack-CUS_abc123")
    body = json.dumps(_wa_payload(f"wamid.{_eid()}")).encode()
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/whatsapp", content=body,
                       headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
    r = client.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sig})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    fn, args = background[0]
    assert fn is recovery.handle_inbound_message
    assert args == ("paystack-CUS_abc123", "I'll pay Friday")


# ─── Retry scheduling ───────────────────────────────────────────────────────
def test_retry_at_validation():
    future = (date.today() + timedelta(days=5)).isoformat()
    assert validate_action({"type": "SCHEDULE_RETRY", "retry_at": future})["retry_at"] == future
    past = (date.today() - timedelta(days=1)).isoformat()
    assert validate_action({"retry_at": past})["retry_at"] is None
    too_far = (date.today() + timedelta(days=365)).isoformat()
    assert validate_action({"retry_at": too_far})["retry_at"] is None
    assert validate_action({"retry_at": "next Friday"})["retry_at"] is None


def test_retry_due_at_defaults_and_is_in_future(_settings):
    _settings.default_retry_days = 3
    due = recovery._retry_due_at({"retry_at": None})
    assert due.date() == date.today() + timedelta(days=3) and due.hour == 8
    today = recovery._retry_due_at({"retry_at": date.today().isoformat()})
    assert today > datetime.now(timezone.utc)


def _stub_turn(monkeypatch, action: dict):
    def fake_run_turn(state):
        return {**state, "reply": "ok", "route": "pay_later", "action": action,
                "history": [], "promises": []}
    monkeypatch.setattr(recovery, "run_turn", fake_run_turn)


def test_schedule_retry_action_queues_a_retry(monkeypatch, no_db):
    retry_day = (date.today() + timedelta(days=2)).isoformat()
    _stub_turn(monkeypatch, validate_action({"type": Action.SCHEDULE_RETRY,
                                             "retry_at": retry_day}))
    scheduled = []
    monkeypatch.setattr(db, "schedule_retry", lambda cid, due: scheduled.append((cid, due)))
    out = recovery.run_turn_locked("demo-retry", "I'll pay on payday",
                                   context={"customer_name": "Ada"})
    assert scheduled and scheduled[0][0] == "demo-retry"
    assert scheduled[0][1].date().isoformat() == retry_day
    assert out["retry_scheduled_for"].startswith(retry_day)


def test_send_payment_link_uses_demo_checkout_without_keys(monkeypatch, no_db):
    _stub_turn(monkeypatch, validate_action({"type": Action.SEND_PAYMENT_LINK}))
    out = recovery.run_turn_locked("demo-link", "", context={"customer_name": "Ada"})
    assert out["payment_link"] == "/demo/checkout/demo-link"


def test_create_payment_link_reuses_cached_link():
    assert payments.create_payment_link(
        {"conversation_id": "c", "payment_link": "https://x/y"}) == "https://x/y"


# ─── Closing the loop ───────────────────────────────────────────────────────
@pytest.fixture
def fake_store(monkeypatch):
    """A tiny in-memory stand-in for the conversation/outcome tables."""
    store = {"conversations": {}, "outcomes": {}, "cancelled": []}
    monkeypatch.setattr(db, "load_conversation", lambda cid: store["conversations"].get(cid))
    monkeypatch.setattr(db, "save_conversation",
                        lambda cid, s: store["conversations"].__setitem__(cid, s))
    monkeypatch.setattr(db, "record_outcome",
                        lambda cid, o, a=0, c="NGN": store["outcomes"].__setitem__(cid, (o, a)))
    monkeypatch.setattr(db, "cancel_retries", lambda cid: store["cancelled"].append(cid))

    @contextlib.contextmanager
    def lock(_cid):
        yield True
    monkeypatch.setattr(db, "conversation_lock", lock)
    return store


def test_payment_success_marks_recovered_and_cancels_retries(fake_store):
    fake_store["conversations"]["demo-paid"] = {
        "conversation_id": "demo-paid", "customer_name": "Ada", "amount": 5000,
        "currency": "NGN", "language": "english", "history": []}
    assert recovery.handle_payment_succeeded("demo-paid") is True
    assert fake_store["outcomes"]["demo-paid"] == ("recovered", 5000.0)
    assert fake_store["cancelled"] == ["demo-paid"]
    state = fake_store["conversations"]["demo-paid"]
    assert state["status"] == "recovered" and "Ada" in state["history"][-1]["content"]


def test_payment_success_for_unknown_conversation_is_ignored(fake_store):
    assert recovery.handle_payment_succeeded("demo-nobody") is False
    assert fake_store["outcomes"] == {}


def test_demo_checkout_only_for_demo_conversations(fake_store, _settings):
    fake_store["conversations"]["demo-x"] = {"conversation_id": "demo-x", "amount": 1}
    assert client.post("/demo/checkout/paystack-CUS_1").status_code == 404
    assert client.post("/demo/checkout/demo-x").json()["status"] == "recovered"
    _settings.demo_mode = False
    assert client.post("/demo/checkout/demo-x").status_code == 404


def test_scheduled_retry_after_recovery_is_cancelled(fake_store, monkeypatch):
    fake_store["conversations"]["demo-r"] = {"conversation_id": "demo-r", "status": "recovered"}
    finished = []
    monkeypatch.setattr(db, "finish_retry", lambda rid, status: finished.append((rid, status)))
    recovery.execute_retry(7, "demo-r")
    assert finished == [(7, "cancelled")]
