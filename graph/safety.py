"""Lightweight safety guards for customer input (#21).

A heuristic prompt-injection detector. It is deliberately conservative — it flags
obvious attempts to override the agent's instructions so the router can hand off to
a human rather than let the model be steered off-task. It is NOT a complete defence
(a production system would add model-level guardrails and moderation), but it stops
the common "ignore your instructions" class cheaply.
"""

from __future__ import annotations

import re

_INJECTION_PATTERNS = [
    r"ignore\s+(?:\w+\s+){0,4}instructions?",
    r"disregard\s+(?:\w+\s+){0,4}(instructions?|above|prompt)",
    r"you are now\b",
    r"forget\s+(everything|all|your instructions|previous)",
    r"system prompt",
    r"reveal\s+(?:\w+\s+){0,3}(prompt|instructions)",
    r"\bact as (a|an|the)\b",
    r"pretend (to be|you are)",
    r"new instructions\s*:",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


def flag_injection(text: str) -> bool:
    """Return True if the message looks like a prompt-injection attempt."""
    if not text:
        return False
    return any(p.search(text) for p in _COMPILED)


def moderate(text: str) -> bool:
    """Return True if OpenAI moderation flags the text (harassment, self-harm, etc.).

    Opt-in via ENABLE_MODERATION (#17); best-effort — returns False on any error so
    moderation never breaks the conversation.
    """
    from app.config import get_settings

    s = get_settings()
    if not (s.enable_moderation and s.openai_api_key and text):
        return False
    try:
        import openai

        result = openai.OpenAI(api_key=s.openai_api_key).moderations.create(
            model="omni-moderation-latest", input=text
        )
        return bool(result.results[0].flagged)
    except Exception:
        return False
