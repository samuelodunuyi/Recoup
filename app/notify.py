"""Human-handoff notifications (#18).

Sends an escalation email when SMTP is configured; otherwise it's a no-op (the
handoff is still recorded to the DB). Configure via SMTP_* env vars — for Gmail,
use an App Password as SMTP_PASSWORD.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger("recoup.notify")


def send_handoff_email(conversation_id: str, customer_name: str, reason: str,
                       last_message: str) -> bool:
    """Email the configured handoff address. Returns True if sent."""
    s = get_settings()
    if not s.smtp_host:
        logger.info("handoff email skipped (SMTP not configured) for %s", conversation_id)
        return False

    msg = EmailMessage()
    msg["Subject"] = f"[Recoup] Human handoff needed — {customer_name} ({reason})"
    msg["From"] = s.smtp_from
    msg["To"] = s.handoff_email
    msg.set_content(
        f"A Recoup conversation needs a human.\n\n"
        f"Conversation: {conversation_id}\n"
        f"Customer: {customer_name}\n"
        f"Reason: {reason}\n"
        f"Last message: {last_message}\n"
    )
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as server:
            server.starttls()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        logger.info("handoff email sent for %s", conversation_id)
        return True
    except Exception as exc:  # never break the request path on email failure
        logger.warning("handoff email failed for %s: %s", conversation_id, exc)
        return False
