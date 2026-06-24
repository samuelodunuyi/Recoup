"""Compile the recovery conversation into a LangGraph StateGraph.

Flow:

    START → router ─┬─(needs_human)─────────────→ escalate ─→ memory → END
                    └─(else)→ decline_intelligence → negotiator → memory → END

The router does policy-based routing; needs_human short-circuits the LLM-backed
Negotiator and emits a deterministic escalation. Everything ends at Memory so the
turn is always recorded.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from graph.actions import Route
from graph.nodes import (
    decline_intelligence_node,
    escalate_node,
    memory_node,
    negotiator_node,
    router_node,
)
from graph.state import RecoveryState


def _route_decider(state: RecoveryState) -> str:
    # Disputes and explicit escalations skip the Negotiator and hand off to a human
    # deterministically — the agent shouldn't try to recover a contested charge.
    if state.get("route") in (Route.NEEDS_HUMAN, Route.DISPUTE):
        return "escalate"
    return "recover"


def build_graph():
    builder = StateGraph(RecoveryState)
    builder.add_node("router", router_node)
    builder.add_node("decline_intelligence", decline_intelligence_node)
    builder.add_node("negotiator", negotiator_node)
    builder.add_node("escalate", escalate_node)
    builder.add_node("memory", memory_node)

    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router",
        _route_decider,
        {"recover": "decline_intelligence", "escalate": "escalate"},
    )
    builder.add_edge("decline_intelligence", "negotiator")
    builder.add_edge("negotiator", "memory")
    builder.add_edge("escalate", "memory")
    builder.add_edge("memory", END)

    return builder.compile()
