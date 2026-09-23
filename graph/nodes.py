"""The four agent nodes: Router, Decline-Intelligence (RAG), Negotiator, Memory.

Each node takes the shared state and returns a partial update. Nodes are kept
independently testable — they call small, mockable helpers rather than reaching
into global I/O directly.
"""

from __future__ import annotations

import logging

from graph import prompts
from graph.actions import Action, Route, validate_action
from graph.state import RecoveryState
from graph.safety import flag_injection, moderate
from graph.trace import traced
from llm import get_client

logger = logging.getLogger("recoup.graph")

# Shared process-wide client (same instance the API uses) so cost accounting
# aggregates across the whole app.
_client = get_client()

# Day-2 fallback strategies, used when the RAG store is empty/unavailable. Day 3's
# ingestion populates `playbook_chunks` and retrieval prefers those.
_HARDCODED_STRATEGIES: dict[str, str] = {
    "insufficient_funds": (
        "The card had insufficient funds. Acknowledge gently, avoid blame, and "
        "suggest retrying right after payday or with another card. Offer a fresh "
        "payment link and propose timing around the next salary date."
    ),
    "card_declined": (
        "The bank declined the card. Reassure the customer it's common, suggest "
        "trying another card or bank, and send a fresh link to retry now."
    ),
    "expired_card": (
        "The card has expired. Ask them to update to a current card and send a "
        "link to do so."
    ),
}
_DEFAULT_STRATEGY = (
    "Acknowledge the failed payment warmly, explain a quick retry usually fixes it, "
    "and offer a fresh payment link with friendly timing."
)


def _retrieve_strategy(decline_code: str, processor: str, query: str) -> tuple[str, list[str]]:
    """Prefer the pgvector store; fall back to hardcoded strategies if it's empty."""
    try:
        from rag.store import search  # imported lazily so Day 2 runs without rag wired

        hits = search(decline_code=decline_code, processor=processor, query=query, k=3)
        if hits:
            strategy = "\n".join(h["content"] for h in hits)
            sources = [h.get("source", "playbook") for h in hits]
            return strategy, sources
    except Exception as exc:  # store not ready yet — fall back, don't crash the graph
        logger.info("RAG store unavailable, using hardcoded strategy: %s", exc)

    return _HARDCODED_STRATEGIES.get(decline_code, _DEFAULT_STRATEGY), ["hardcoded"]


# ─── Node 1: Router / Classifier ────────────────────────────────────────────
@traced
def router_node(state: RecoveryState) -> dict:
    """Policy-based routing: classify the customer's conversational state."""
    # Initial failed-payment event (no customer message) is always a new failure.
    if not state.get("customer_message"):
        return {"route": Route.NEW_FAILURE}

    # Safety (#21, #17): prompt-injection attempts and moderation-flagged content
    # (when enabled) go straight to a human.
    if flag_injection(state["customer_message"]) or moderate(state["customer_message"]):
        logger.warning("safety guard triggered; escalating conversation %s",
                       state.get("conversation_id"))
        return {"route": Route.NEEDS_HUMAN}

    # Give the classifier recent history so short follow-ups ("ok, I'm ready now")
    # are routed in context rather than in isolation.
    history = state.get("history") or []
    if history:
        recent = "\n".join(f"{h['role']}: {h['content']}" for h in history[-4:])
        content = f"Conversation so far:\n{recent}\n\nLatest customer message: {state['customer_message']}"
    else:
        content = state["customer_message"]

    parsed, _ = _client.complete_json(
        system=prompts.ROUTER_SYSTEM,
        messages=[{"role": "user", "content": content}],
        conversation_id=state.get("conversation_id", "default"),
    )
    route = parsed.get("route", Route.NEW_FAILURE) if isinstance(parsed, dict) else None
    if route not in Route.ALL:
        # Unknown/garbled classification → keep the customer in the conversation
        # rather than escalating to a human (a greeting must not trigger a handoff).
        route = Route.NEW_FAILURE
    return {"route": route}


# ─── Node 2: Decline-Intelligence (RAG) ─────────────────────────────────────
@traced
def decline_intelligence_node(state: RecoveryState) -> dict:
    """Retrieve the right recovery strategy for this decline code + processor."""
    query = state.get("customer_message") or state.get("decline_code", "")
    strategy, sources = _retrieve_strategy(
        decline_code=state.get("decline_code", ""),
        processor=state.get("processor", ""),
        query=query,
    )
    return {"strategy": strategy, "strategy_sources": sources}


# ─── Node 3: Negotiator / Responder ─────────────────────────────────────────
@traced
def negotiator_node(state: RecoveryState) -> dict:
    """Generate the in-thread reply and a structured action."""
    parsed, _ = _client.complete_json(
        system=prompts.NEGOTIATOR_SYSTEM,
        messages=[{"role": "user", "content": prompts.negotiator_user_payload(state)}],
        conversation_id=state.get("conversation_id", "default"),
    )
    if not isinstance(parsed, dict):
        parsed = {}
    reply = parsed.get("reply", "") if isinstance(parsed.get("reply"), str) else ""
    # #16 schema-validate the structured action (coerces bad/partial output safely).
    action = validate_action(parsed.get("action"))
    return {"reply": reply, "action": action}


# ─── Node 3b: deterministic escalation (skips the LLM for dispute/needs_human) ─
_ESCALATION_REPLY = {
    ("default", "english"): (
        "Thanks for letting us know — I'm passing this to a member of our team "
        "who'll follow up with you shortly."
    ),
    ("default", "pidgin"): (
        "Thank you say you tell us o. I don pass am give our team, dem go reach "
        "you sharp sharp."
    ),
    ("dispute", "english"): (
        "I understand your concern about this charge — let me connect you with "
        "someone on our team who can look into it and sort it out for you."
    ),
    ("dispute", "pidgin"): (
        "I sabi say this charge dey worry you. Make I connect you with our team "
        "wey go check am well well and sort am out for you, no wahala."
    ),
    ("default", "spanish"): (
        "Gracias por avisarnos. Voy a pasar esto a un miembro de nuestro equipo "
        "que se pondrá en contacto contigo muy pronto."
    ),
    ("dispute", "spanish"): (
        "Entiendo tu preocupación por este cargo. Deja que te ponga en contacto con "
        "alguien de nuestro equipo que pueda revisarlo y resolverlo."
    ),
    ("default", "french"): (
        "Merci de nous avoir prévenus. Je transmets ceci à un membre de notre équipe "
        "qui vous recontactera très bientôt."
    ),
    ("dispute", "french"): (
        "Je comprends votre inquiétude concernant ce prélèvement. Je vous mets en "
        "relation avec un membre de notre équipe qui pourra l'examiner et le résoudre."
    ),
    ("default", "swahili"): (
        "Asante kwa kutujulisha. Nitampa mtu wa timu yetu suala hili, "
        "atawasiliana nawe hivi karibuni."
    ),
    ("dispute", "swahili"): (
        "Naelewa wasiwasi wako kuhusu malipo haya. Nitakuunganisha na mtu wa timu "
        "yetu atakayeliangalia na kulitatua."
    ),
}


@traced
def escalate_node(state: RecoveryState) -> dict:
    language = state.get("language", "english")
    kind = "dispute" if state.get("route") == Route.DISPUTE else "default"
    reply = _ESCALATION_REPLY.get((kind, language), _ESCALATION_REPLY[("default", "english")])
    return {
        "reply": reply,
        "action": {"type": Action.ESCALATE_TO_HUMAN, "schedule_for": None, "promise": None,
                   "retry_at": None},
    }


# ─── Node 4: Memory ─────────────────────────────────────────────────────────
# Cap retained turns so long conversations don't grow the prompt / stored state
# unbounded. Promises persist separately, so older commitments aren't lost.
_MAX_HISTORY_MESSAGES = 20


@traced
def memory_node(state: RecoveryState) -> dict:
    """Append this turn to history and record any captured promise."""
    history = list(state.get("history") or [])
    if state.get("customer_message"):
        history.append({"role": "customer", "content": state["customer_message"]})
    if state.get("reply"):
        history.append({"role": "agent", "content": state["reply"]})
    history = history[-_MAX_HISTORY_MESSAGES:]  # keep only the most recent turns

    promises = list(state.get("promises") or [])
    promise = (state.get("action") or {}).get("promise")
    if promise and promise not in promises:
        promises.append(promise)

    # Remember once a payment link has been sent, so we don't re-send it every turn.
    link_sent = bool(state.get("link_sent")) or \
        (state.get("action") or {}).get("type") == Action.SEND_PAYMENT_LINK

    return {"history": history, "promises": promises, "link_sent": link_sent}
