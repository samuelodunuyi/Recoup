"""Recovery orchestration: one conversation turn plus everything around it.

Every entry point — the browser demo (`/chat`), processor webhooks, inbound
WhatsApp messages, and the scheduled-retry worker — goes through `run_turn_locked`,
so locking, spend caps, persistence, payment links, retries, outcomes, handoffs and
delivery behave identically however a turn starts.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date, datetime, time, timedelta, timezone

from app import db, notify, payments, whatsapp
from app.config import get_settings
from app.context import request_id_var
from graph import run_turn
from graph.actions import Action
from graph.safety import flag_injection
from llm import LLMError, get_client

logger = logging.getLogger("recoup.recovery")

llm_client = get_client()

# Bounded worker pool so a turn can be given a hard deadline (#8).
_TURN_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# Retries fire at 08:00 UTC (09:00 WAT) on the agreed day — a sensible hour to
# message someone and, around payday, when funds have usually landed.
_RETRY_HOUR_UTC = 8

_THANK_YOU = {
    "english": "Payment received — thank you, {name}! You're all set. 🎉",
    "pidgin": "We don receive your payment — thank you well well, {name}! Everything dey set. 🎉",
    "spanish": "¡Pago recibido, gracias {name}! Ya está todo listo. 🎉",
    "french": "Paiement reçu — merci {name} ! Tout est en ordre. 🎉",
    "swahili": "Malipo yamepokelewa — asante {name}! Kila kitu kiko sawa. 🎉",
}


class TurnError(Exception):
    """A turn couldn't run; `status` is the HTTP status the API should return."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _best_effort(what: str, fn, *args) -> None:
    """Side effects (outcomes, retries, contacts) must never fail the turn itself."""
    try:
        fn(*args)
    except Exception as exc:
        logger.warning("could not %s: %s", what, exc)


# ─── Spend governance ───────────────────────────────────────────────────────
def daily_budget_exceeded() -> bool:
    """True once the global rolling-24h spend cap is reached, so rotating
    conversation ids can't bypass the per-conversation cap."""
    cap = get_settings().max_daily_cost_usd
    if cap <= 0:
        return False
    try:
        spent = db.cost_last_24h() if db.enabled() else llm_client.cost_last_24h()
    except Exception as exc:
        logger.warning("could not read daily spend (using in-process total): %s", exc)
        spent = llm_client.cost_last_24h()
    if spent >= cap:
        logger.warning("daily LLM budget reached ($%.4f >= $%.2f)", spent, cap)
        return True
    return False


def _conversation_spend(conversation_id: str) -> float:
    # Persisted spend is global across workers; fall back to this process's report.
    if db.enabled():
        return db.conversation_cost(conversation_id)
    return llm_client.cost_report(conversation_id).get("total_cost_usd", 0.0)


# ─── Turn entry point ───────────────────────────────────────────────────────
def run_turn_locked(conversation_id: str, message: str = "", *,
                    context: dict | None = None, trigger: str | None = None,
                    deliver: bool = False) -> dict:
    """Run one turn under the conversation lock with a hard deadline.

    `context` seeds a new conversation (ignored for existing ones, unless the
    trigger is a fresh payment failure, which refreshes the payment details).
    `deliver` sends the reply to the customer's WhatsApp when the channel is live.
    Raises TurnError on budget, contention, timeout, or failure.
    """
    if daily_budget_exceeded():
        raise TurnError(503, "daily usage limit reached, please try again later")
    # Serialise concurrent turns for this conversation so memory isn't clobbered (#3).
    with db.conversation_lock(conversation_id) as acquired:
        if not acquired:
            raise TurnError(409, "a message for this conversation is already being processed")
        abandoned = threading.Event()
        future = _TURN_EXECUTOR.submit(_execute, conversation_id, message, context or {},
                                       trigger, deliver, abandoned)
        try:
            return future.result(timeout=get_settings().request_timeout_seconds)
        except FuturesTimeout:
            # The worker can't be killed; tell it not to persist once it finishes,
            # since the lock is released and a newer turn may already have saved.
            abandoned.set()
            raise TurnError(504, "recovery turn timed out")


def _initial_state(conversation_id: str, context: dict) -> dict:
    return {
        "conversation_id": conversation_id,
        "customer_name": context.get("customer_name", "there"),
        "decline_code": context.get("decline_code", "insufficient_funds"),
        "processor": context.get("processor", "paystack"),
        "language": context.get("language", "english"),
        "amount": context.get("amount", 0.0),
        "currency": context.get("currency", "NGN"),
        "phone": context.get("phone"),
        "email": context.get("email"),
        "authorization_code": context.get("authorization_code"),
        "status": "open",
        "history": [],
        "promises": [],
    }


def _refresh_for_new_failure(state: dict, context: dict) -> None:
    """A returning customer's new failed charge: new amount/reason, fresh link."""
    for key in ("decline_code", "processor", "amount", "currency", "phone", "email",
                "authorization_code"):
        if context.get(key) is not None:
            state[key] = context[key]
    state.update(status="open", payment_link=None, link_sent=False, promises=[])


def _retry_due_at(action: dict) -> datetime:
    """When to run a SCHEDULE_RETRY: the model's validated date, else a default."""
    today = date.today()
    retry_day = (date.fromisoformat(action["retry_at"]) if action.get("retry_at")
                 else today + timedelta(days=get_settings().default_retry_days))
    due = datetime.combine(retry_day, time(_RETRY_HOUR_UTC), tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return due if due > now else now + timedelta(hours=1)  # "today" but past 08:00


def _execute(conversation_id: str, message: str, context: dict, trigger: str | None,
             deliver: bool, abandoned: threading.Event) -> dict:
    prior = None
    try:
        prior = db.load_conversation(conversation_id)
    except Exception as exc:
        logger.warning("could not load memory (continuing fresh): %s", exc)

    if prior is None:
        # The name is interpolated into the Negotiator prompt, so screen it with the
        # same injection guard applied to customer messages.
        if flag_injection(context.get("customer_name", "")):
            raise TurnError(422, "invalid customer_name")
        state = _initial_state(conversation_id, context)
    else:
        state = prior
        if trigger == "payment_failed":
            _refresh_for_new_failure(state, context)

    state["customer_message"] = message
    state["trigger"] = trigger

    # Safety (#21): stop spending on a runaway conversation.
    if _conversation_spend(conversation_id) >= get_settings().max_cost_per_conversation:
        logger.warning("conversation %s hit spend cap", conversation_id)
        reply = "Thanks — let me bring in a teammate to help you finish this up."
        handoff(conversation_id, state.get("customer_name", "there"), "spend_cap", message)
        if deliver:
            whatsapp.send_reply(state, reply, None, first_contact=False)
        return {
            "reply": reply, "route": "needs_human",
            "action": {"type": Action.ESCALATE_TO_HUMAN},
            "payment_link": None, "promises": state.get("promises", []),
            "strategy_sources": [], "retry_scheduled_for": None,
            "cost": llm_client.cost_report(conversation_id),
        }

    try:
        result = run_turn(state)
    except LLMError as exc:
        logger.warning("all LLM providers failed: %s", exc)
        raise TurnError(502, "LLM providers unavailable") from exc
    except Exception as exc:
        # Full traceback goes to the server log; the caller gets only the request id.
        logger.exception("graph run failed")
        raise TurnError(500, f"internal error (request id {request_id_var.get() or 'n/a'})") \
            from exc

    if abandoned.is_set():
        logger.warning("turn for %s finished after timeout; not persisting", conversation_id)
        return {}

    action = result.get("action") or {}
    payment_link = None
    if action.get("type") == Action.SEND_PAYMENT_LINK:
        payment_link = payments.create_payment_link(result)
        result["payment_link"] = payment_link  # cached so repeat sends reuse it

    try:
        db.save_conversation(conversation_id, result)
    except Exception as exc:
        logger.warning("could not persist memory: %s", exc)

    retry_at = None
    if prior is None or trigger == "payment_failed":
        _best_effort("record outcome", db.record_outcome, conversation_id, "pending",
                     result.get("amount", 0.0), result.get("currency", "NGN"))
    if action.get("type") == Action.SCHEDULE_RETRY:
        due = _retry_due_at(action)
        retry_at = due.isoformat()
        _best_effort("schedule retry", db.schedule_retry, conversation_id, due)
        _best_effort("record outcome", db.record_outcome, conversation_id, "scheduled",
                     result.get("amount", 0.0), result.get("currency", "NGN"))
    elif action.get("type") == Action.ESCALATE_TO_HUMAN:
        # #18 record a human handoff (+ optional email) whenever we escalate.
        handoff(conversation_id, result.get("customer_name", "there"),
                result.get("route", "escalation"), message)
        _best_effort("record outcome", db.record_outcome, conversation_id, "escalated",
                     result.get("amount", 0.0), result.get("currency", "NGN"))

    if deliver:
        first_contact = prior is None or trigger == "payment_failed"
        whatsapp.send_reply(result, result.get("reply", ""), payment_link, first_contact)

    return {
        "reply": result.get("reply", ""),
        "route": result.get("route"),
        "action": action,
        "payment_link": payment_link,
        "promises": result.get("promises", []),
        "strategy_sources": result.get("strategy_sources", []),
        "retry_scheduled_for": retry_at,
        "cost": llm_client.cost_report(conversation_id),
    }


def handoff(conversation_id: str, customer_name: str, reason: str, message: str) -> None:
    """Record a human handoff to the DB and try to email it (best-effort)."""
    _best_effort("record handoff", db.record_handoff, conversation_id, customer_name,
                 reason, message)
    notify.send_handoff_email(conversation_id, customer_name, reason, message)


# ─── Processor events ───────────────────────────────────────────────────────
def handle_payment_failed(evt: payments.PaymentFailure) -> None:
    """Start (or restart) the recovery conversation for a failed charge and send the
    opening message. Runs in the background after the webhook has returned."""
    phone = whatsapp.normalize_phone(evt.phone)
    if phone:
        _best_effort("save contact", db.upsert_contact, phone, evt.conversation_id)
    context = {
        "customer_name": evt.customer_name, "decline_code": evt.decline_code,
        "processor": evt.processor, "amount": evt.amount, "currency": evt.currency,
        "phone": phone, "email": evt.email, "authorization_code": evt.authorization_code,
    }
    try:
        run_turn_locked(evt.conversation_id, "", context=context,
                        trigger="payment_failed", deliver=True)
    except TurnError as exc:
        logger.warning("could not open recovery for %s: %s", evt.conversation_id, exc.detail)


def handle_payment_succeeded(conversation_id: str, amount: float | None = None,
                             currency: str | None = None) -> bool:
    """Close the loop: mark recovered, cancel retries, thank the customer.

    Returns False when the payment doesn't belong to a recovery conversation (an
    ordinary successful charge), which is then ignored.
    """
    state = db.load_conversation(conversation_id)
    if state is None:
        return False
    amount = amount if amount else float(state.get("amount") or 0)
    currency = currency or state.get("currency", "NGN")
    db.record_outcome(conversation_id, "recovered", amount, currency)
    db.cancel_retries(conversation_id)
    if state.get("status") == "recovered":
        return True

    name = state.get("customer_name") or "there"
    thanks = _THANK_YOU.get(state.get("language", "english"), _THANK_YOU["english"])
    thanks = thanks.format(name=name)
    with db.conversation_lock(conversation_id) as acquired:
        # If a turn is mid-flight, skip the state update; the outcome is recorded.
        if acquired:
            state = db.load_conversation(conversation_id) or state
            state["status"] = "recovered"
            state["history"] = [*(state.get("history") or []),
                                {"role": "agent", "content": thanks}][-20:]
            db.save_conversation(conversation_id, state)
    whatsapp.send_reply(state, thanks, None, first_contact=False)
    return True


def handle_inbound_message(conversation_id: str, text: str, attempts: int = 5) -> None:
    """Run a turn for a customer's WhatsApp reply and deliver the answer. Retries
    briefly if another turn for the same conversation is still running."""
    for attempt in range(attempts):
        try:
            run_turn_locked(conversation_id, text, deliver=True)
            return
        except TurnError as exc:
            if exc.status == 409 and attempt < attempts - 1:
                _time.sleep(2)
                continue
            logger.warning("inbound message for %s not processed: %s",
                           conversation_id, exc.detail)
            return


# ─── Scheduled retries ──────────────────────────────────────────────────────
def execute_retry(retry_id: int, conversation_id: str) -> None:
    """Run one due retry: re-charge a saved card if possible, else remind the
    customer with a fresh link, as agreed."""
    state = db.load_conversation(conversation_id)
    if state is None or state.get("status") == "recovered":
        db.finish_retry(retry_id, "cancelled")
        return
    if payments.charge_saved_card(state):
        handle_payment_succeeded(conversation_id)
        db.finish_retry(retry_id, "done")
        return
    try:
        run_turn_locked(conversation_id, "", trigger="scheduled_retry", deliver=True)
        db.finish_retry(retry_id, "done")
    except TurnError as exc:
        # Busy conversation: put it back so the next poll picks it up again.
        db.finish_retry(retry_id, "pending" if exc.status == 409 else "failed")
        if exc.status != 409:
            logger.warning("retry %s for %s failed: %s", retry_id, conversation_id, exc.detail)


def run_due_retries() -> int:
    """Claim and execute every retry that has come due. Returns how many ran."""
    due = db.claim_due_retries()
    for retry_id, conversation_id in due:
        try:
            execute_retry(retry_id, conversation_id)
        except Exception:
            logger.exception("retry %s crashed", retry_id)
            _best_effort("mark retry failed", db.finish_retry, retry_id, "failed")
    return len(due)
