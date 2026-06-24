"""Per-turn driver around the compiled graph.

`run_turn` takes the prior persisted state plus this turn's customer message,
runs one pass through the graph, and returns the updated state. The graph is
compiled once at import.
"""

from __future__ import annotations

import uuid

from graph.graph import build_graph
from graph.state import RecoveryState

_GRAPH = build_graph()


def run_turn(state: RecoveryState) -> RecoveryState:
    """Run one turn. `state` carries context + memory; returns the merged result."""
    state["run_id"] = uuid.uuid4().hex[:12]
    result = _GRAPH.invoke(state)
    return result  # type: ignore[return-value]
