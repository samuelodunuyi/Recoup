"""WhatsApp Cloud API channel: send replies, verify and parse inbound webhooks.

Off unless WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID are set; replies are
then only returned over HTTP (the browser demo). WhatsApp only allows a business to
open a conversation with an approved *template*, so the first message after a failed
payment uses WHATSAPP_OPENING_TEMPLATE; replies inside the customer-service window
are sent as free text.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

import httpx

from app.config import get_settings

logger = logging.getLogger("recoup.whatsapp")

_MAX_TEXT = 4096  # WhatsApp text-message limit


@dataclass
class InboundMessage:
    message_id: str
    phone: str
    text: str


def configured() -> bool:
    s = get_settings()
    return bool(s.whatsapp_access_token and s.whatsapp_phone_number_id)


def normalize_phone(raw: str | None) -> str | None:
    """Digits-only international format (WhatsApp's `wa_id`), or None if implausible."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0"):
        digits = get_settings().default_country_code + digits[1:]
    return digits if 8 <= len(digits) <= 15 else None


def verify_signature(body: bytes, header: str) -> bool:
    """Meta signs webhooks as `sha256=<HMAC-SHA256(app_secret, body)>`."""
    secret = get_settings().whatsapp_app_secret
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):].encode(), expected.encode())


def parse_inbound(payload: dict) -> list[InboundMessage]:
    """Extract customer text messages from a Cloud API webhook (ignores statuses)."""
    messages: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for msg in (change.get("value") or {}).get("messages") or []:
                if msg.get("type") != "text":
                    continue
                phone = normalize_phone(msg.get("from"))
                text = ((msg.get("text") or {}).get("body") or "").strip()
                if phone and text and msg.get("id"):
                    messages.append(InboundMessage(msg["id"], phone, text[:1000]))
    return messages


def _post(payload: dict) -> bool:
    s = get_settings()
    url = (f"https://graph.facebook.com/{s.whatsapp_api_version}/"
           f"{s.whatsapp_phone_number_id}/messages")
    try:
        resp = httpx.post(url, json={"messaging_product": "whatsapp", **payload},
                          headers={"Authorization": f"Bearer {s.whatsapp_access_token}"},
                          timeout=15.0)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("WhatsApp send failed: %s", exc)
        return False


def send_reply(state: dict, reply: str, payment_link: str | None,
               first_contact: bool) -> bool:
    """Deliver the agent's reply to the customer's WhatsApp. Returns True if sent."""
    phone = state.get("phone")
    if not (configured() and phone):
        return False
    s = get_settings()

    if first_contact and s.whatsapp_opening_template:
        amount = f"{state.get('amount', 0):,.2f} {state.get('currency', '')}".strip()
        params = [state.get("customer_name") or "there", amount, payment_link or "-"]
        return _post({
            "to": phone,
            "type": "template",
            "template": {
                "name": s.whatsapp_opening_template,
                "language": {"code": s.whatsapp_template_language},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in params],
                }],
            },
        })
    if first_contact:
        logger.warning("no WHATSAPP_OPENING_TEMPLATE set; a free-text first message is "
                       "only delivered if the customer messaged in the last 24h")

    body = reply + (f"\n\n💳 Pay here: {payment_link}" if payment_link else "")
    return _post({"to": phone, "type": "text",
                  "text": {"body": body[:_MAX_TEXT], "preview_url": bool(payment_link)}})
