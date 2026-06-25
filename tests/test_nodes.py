"""Tests for the graph nodes: routing, action coercion, memory, escalation.

Nodes are exercised in isolation with the shared LLM client's `complete_json`
monkeypatched, so no network or DB is needed.
"""

from __future__ import annotations

import pytest

from graph import nodes
from graph.actions import Action, Route


def _patch_json(monkeypatch, payload):
    """Make the shared client's complete_json return a fixed payload."""
    monkeypatch.setattr(nodes._client, "complete_json", lambda *a, **k: (payload, None))


def test_router_initial_event_is_new_failure(monkeypatch):
    # No customer message → new_failure without any LLM call.
    called = {"n": 0}
    monkeypatch.setattr(nodes._client, "complete_json",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ({}, None))
    out = nodes.router_node({"customer_message": ""})
    assert out["route"] == Route.NEW_FAILURE
    assert called["n"] == 0


def test_router_unknown_route_falls_back_to_new_failure(monkeypatch):
    # An unknown/garbled classification must NOT escalate — keep the conversation.
    _patch_json(monkeypatch, {"route": "banana"})
    out = nodes.router_node({"customer_message": "hello", "history": []})
    assert out["route"] == Route.NEW_FAILURE


def test_router_valid_route_passthrough(monkeypatch):
    _patch_json(monkeypatch, {"route": Route.PAY_LATER})
    out = nodes.router_node({"customer_message": "friday", "history": []})
    assert out["route"] == Route.PAY_LATER


def test_negotiator_coerces_string_action(monkeypatch):
    # Model returns action as a bare string instead of an object — must not crash.
    _patch_json(monkeypatch, {"reply": "here", "action": "SEND_PAYMENT_LINK"})
    out = nodes.negotiator_node({})
    assert out["action"]["type"] == Action.SEND_PAYMENT_LINK
    assert out["reply"] == "here"


def test_negotiator_invalid_action_type_becomes_none(monkeypatch):
    _patch_json(monkeypatch, {"reply": "x", "action": {"type": "NONSENSE"}})
    out = nodes.negotiator_node({})
    assert out["action"]["type"] == Action.NONE


def test_negotiator_non_dict_parsed_is_safe(monkeypatch):
    _patch_json(monkeypatch, ["unexpected", "list"])
    out = nodes.negotiator_node({})
    assert out["reply"] == ""
    assert out["action"]["type"] == Action.NONE


def test_memory_captures_promise_and_history():
    state = {
        "customer_message": "I'll pay Friday",
        "reply": "Great, Friday it is.",
        "action": {"type": Action.SCHEDULE_RETRY, "promise": "pay Friday"},
        "history": [],
        "promises": [],
    }
    out = nodes.memory_node(state)
    assert out["promises"] == ["pay Friday"]
    assert [h["role"] for h in out["history"]] == ["customer", "agent"]


def test_escalation_localises_by_language():
    pidgin = nodes.escalate_node({"language": "pidgin"})["reply"]
    english = nodes.escalate_node({"language": "english"})["reply"]
    assert pidgin != english
    assert "dem" in pidgin            # a Pidgin marker
    assert "team" in english


def test_dispute_escalation_is_distinct():
    from graph.actions import Route
    generic = nodes.escalate_node({"language": "english"})["reply"]
    dispute = nodes.escalate_node({"language": "english", "route": Route.DISPUTE})["reply"]
    assert dispute != generic
    assert "charge" in dispute        # dispute-specific, acknowledges the charge
