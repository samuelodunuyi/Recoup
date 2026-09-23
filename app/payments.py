"""Payment-processor integration: webhook verification + parsing, payment links,
and saved-card retries for Paystack and Flutterwave.

Everything degrades to demo behaviour when the processor keys aren't configured:
payment links point at the built-in demo checkout, and retries fall back to a
reminder message. Processor payloads are normalised into two small event types so
the recovery flow never touches processor-specific shapes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.config import get_settings

logger = logging.getLogger("recoup.payments")

_PAYSTACK_API = "https://api.paystack.co"
_FLUTTERWAVE_API = "https://api.flutterwave.com/v3"
_HTTP_TIMEOUT = 15.0


@dataclass
class PaymentFailure:
    event_id: str
    conversation_id: str
    processor: str                  # paystack | flutterwave | mobile_money
    decline_code: str
    amount: float                   # major units (e.g. naira, not kobo)
    currency: str
    customer_name: str = "there"
    phone: str | None = None
    email: str | None = None
    authorization_code: str | None = None  # Paystack reusable card, for retries


@dataclass
class PaymentSuccess:
    event_id: str
    conversation_id: str
    amount: float
    currency: str


# ─── Normalisation helpers ──────────────────────────────────────────────────
def safe_conversation_id(raw: str) -> str:
    """Reduce an arbitrary processor identifier to the API's conversation-id shape."""
    return re.sub(r"[^A-Za-z0-9_-]", "", raw)[:64]


def safe_name(raw: str | None) -> str:
    """Keep only characters allowed in customer names (see ChatRequest)."""
    cleaned = re.sub(r"[^\w .'-]", "", (raw or "").strip())[:60].strip()
    return cleaned or "there"


def decline_code_from_text(text: str | None, channel: str = "") -> str:
    """Map a processor's free-text failure reason onto our decline codes."""
    t = (text or "").lower()
    if "insufficient" in t or "not sufficient" in t:
        return "insufficient_funds"
    if "expired" in t:
        return "expired_card"
    if "do not honor" in t or "do not honour" in t:
        return "do_not_honour"
    if "limit" in t:
        return "transaction_limit"
    if "mobile" in channel and ("timeout" in t or "timed out" in t or not t):
        return "mobile_money_timeout"
    if "declin" in t:
        return "card_declined"
    # Most recurring-charge failures are balance-related; it's the safest default.
    return "insufficient_funds"


def _major_units(minor: object) -> float:
    try:
        return round(float(minor) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


def _currency(raw: object) -> str:
    c = str(raw or "NGN").upper()
    return c if re.fullmatch(r"[A-Z]{3}", c) else "NGN"


# ─── Paystack ───────────────────────────────────────────────────────────────
def verify_paystack(body: bytes, signature: str) -> bool:
    """Paystack signs each webhook with HMAC-SHA512 of the body using the secret key."""
    key = get_settings().paystack_secret_key
    if not key or not signature:
        return False
    expected = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(signature.encode(), expected.encode())


def parse_paystack(payload: dict) -> PaymentFailure | PaymentSuccess | None:
    """Normalise a Paystack webhook. Returns None for events we don't act on."""
    event = payload.get("event")
    data = payload.get("data") or {}
    customer = data.get("customer") or {}
    customer_code = safe_conversation_id(str(customer.get("customer_code") or ""))

    if event == "invoice.payment_failed":
        if not customer_code:
            return None
        txn = data.get("transaction") or {}
        auth = data.get("authorization") or {}
        channel = str(auth.get("channel") or "")
        amount_minor = data.get("amount") or txn.get("amount")
        key = data.get("invoice_code") or txn.get("reference") or data.get("id")
        return PaymentFailure(
            event_id=f"paystack:{event}:{key}",
            conversation_id=f"paystack-{customer_code}",
            processor="mobile_money" if "mobile" in channel else "paystack",
            decline_code=decline_code_from_text(
                txn.get("gateway_response") or data.get("description"), channel),
            amount=_major_units(amount_minor),
            currency=_currency(txn.get("currency") or data.get("currency")),
            customer_name=safe_name(customer.get("first_name")),
            phone=customer.get("phone") or None,
            email=customer.get("email") or None,
            authorization_code=(auth.get("authorization_code")
                                if auth.get("reusable") else None),
        )

    if event == "charge.success":
        metadata = data.get("metadata")
        cid = metadata.get("conversation_id") if isinstance(metadata, dict) else None
        cid = safe_conversation_id(str(cid)) if cid else (
            f"paystack-{customer_code}" if customer_code else "")
        if not cid:
            return None
        return PaymentSuccess(
            event_id=f"paystack:{event}:{data.get('id') or data.get('reference')}",
            conversation_id=cid,
            amount=_major_units(data.get("amount")),
            currency=_currency(data.get("currency")),
        )
    return None


# ─── Flutterwave ────────────────────────────────────────────────────────────
def verify_flutterwave(verif_hash: str) -> bool:
    """Flutterwave echoes the dashboard-configured secret hash in `verif-hash`."""
    expected = get_settings().flutterwave_secret_hash
    if not expected or not verif_hash:
        return False
    return hmac.compare_digest(verif_hash.encode(), expected.encode())


def _conversation_from_tx_ref(tx_ref: str) -> str | None:
    """Our checkout tx_refs look like `recoup.<conversation_id>.<nonce>`."""
    parts = tx_ref.split(".")
    if len(parts) == 3 and parts[0] == "recoup":
        return safe_conversation_id(parts[1]) or None
    return None


def parse_flutterwave(payload: dict) -> PaymentFailure | PaymentSuccess | None:
    if payload.get("event") != "charge.completed":
        return None
    data = payload.get("data") or {}
    customer = data.get("customer") or {}
    status = str(data.get("status") or "").lower()
    cid = _conversation_from_tx_ref(str(data.get("tx_ref") or ""))
    if not cid and customer.get("id"):
        cid = f"flutterwave-{safe_conversation_id(str(customer['id']))}"
    if not cid:
        return None
    event_id = f"flutterwave:{data.get('id') or data.get('flw_ref')}"
    amount = float(data.get("amount") or 0)  # Flutterwave amounts are major units
    currency = _currency(data.get("currency"))

    if status == "successful":
        return PaymentSuccess(event_id=event_id, conversation_id=cid,
                              amount=amount, currency=currency)
    if status == "failed":
        payment_type = str(data.get("payment_type") or "")
        return PaymentFailure(
            event_id=event_id,
            conversation_id=cid,
            processor="mobile_money" if "mobile" in payment_type else "flutterwave",
            decline_code=decline_code_from_text(data.get("processor_response"), payment_type),
            amount=amount,
            currency=currency,
            customer_name=safe_name((customer.get("name") or "").split(" ")[0]),
            phone=customer.get("phone_number") or None,
            email=customer.get("email") or None,
        )
    return None


# ─── Payment links ──────────────────────────────────────────────────────────
def demo_link(conversation_id: str) -> str:
    """The built-in simulated checkout (see /demo/checkout in app.main)."""
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/demo/checkout/{quote(conversation_id, safe='')}"


def create_payment_link(state: dict) -> str:
    """A hosted-checkout URL for this conversation's outstanding amount.

    Uses the matching processor when its key is configured and the customer's
    email is known (both processors require one); otherwise the demo checkout.
    The link is cached in the conversation state, so repeat sends reuse it.
    """
    if state.get("payment_link"):
        return state["payment_link"]
    s = get_settings()
    cid = state["conversation_id"]
    email = state.get("email")
    amount = float(state.get("amount") or 0)
    if not email or amount <= 0:
        return demo_link(cid)

    use_flutterwave = bool(s.flutterwave_secret_key) and (
        state.get("processor") == "flutterwave" or not s.paystack_secret_key)
    try:
        if use_flutterwave:
            return _flutterwave_link(state, email, amount)
        if s.paystack_secret_key:
            return _paystack_link(state, email, amount)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("payment link creation failed for %s, using demo link: %s", cid, exc)
    return demo_link(cid)


def _paystack_link(state: dict, email: str, amount: float) -> str:
    resp = httpx.post(
        f"{_PAYSTACK_API}/transaction/initialize",
        headers={"Authorization": f"Bearer {get_settings().paystack_secret_key}"},
        json={
            "email": email,
            "amount": int(round(amount * 100)),  # kobo / pesewas / cents
            "currency": state.get("currency") or "NGN",
            "reference": f"recoup-{secrets.token_hex(8)}",
            # Returned on charge.success, which is how the payment is matched back.
            "metadata": {"conversation_id": state["conversation_id"]},
        },
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["data"]["authorization_url"]


def _flutterwave_link(state: dict, email: str, amount: float) -> str:
    s = get_settings()
    body = {
        "tx_ref": f"recoup.{state['conversation_id']}.{secrets.token_hex(4)}",
        "amount": amount,
        "currency": state.get("currency") or "NGN",
        "customer": {"email": email, "name": state.get("customer_name", "")},
    }
    if s.payment_redirect_url:
        body["redirect_url"] = s.payment_redirect_url
    if state.get("phone"):
        body["customer"]["phonenumber"] = state["phone"]
    resp = httpx.post(
        f"{_FLUTTERWAVE_API}/payments",
        headers={"Authorization": f"Bearer {s.flutterwave_secret_key}"},
        json=body,
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["data"]["link"]


# ─── Scheduled retry: re-charge a saved card ────────────────────────────────
def charge_saved_card(state: dict) -> bool:
    """Re-attempt the charge on the customer's saved Paystack authorization.

    Returns True only when Paystack reports the charge succeeded. Needs the secret
    key, a reusable authorization code (from the failure webhook) and an email.
    """
    s = get_settings()
    auth_code, email = state.get("authorization_code"), state.get("email")
    amount = float(state.get("amount") or 0)
    if not (s.paystack_secret_key and auth_code and email and amount > 0):
        return False
    try:
        resp = httpx.post(
            f"{_PAYSTACK_API}/transaction/charge_authorization",
            headers={"Authorization": f"Bearer {s.paystack_secret_key}"},
            json={
                "authorization_code": auth_code,
                "email": email,
                "amount": int(round(amount * 100)),
                "currency": state.get("currency") or "NGN",
                "reference": f"recoup-retry-{secrets.token_hex(8)}",
                "metadata": {"conversation_id": state["conversation_id"]},
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("status") == "success"
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("saved-card retry failed for %s: %s", state["conversation_id"], exc)
        return False
