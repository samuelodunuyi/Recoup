"""LLM-as-judge: score the agent's reply on tone and language appropriateness.

Uses the same provider-agnostic client as the agent, so the judge inherits
fallback. Scoring is 0.0–1.0; the judge also checks that Pidgin was used when the
scenario expected it.
"""

from __future__ import annotations

from llm import LLMClient, LLMError

_JUDGE_SYSTEM = """You are an evaluation judge for a payment-recovery agent. Given \
the customer context, the expected tone notes, the expected language, and the \
agent's reply, score the reply.

Return JSON:
{
  "score": <float 0.0-1.0>,
  "language_ok": <true if the reply is in the expected language; for "pidgin" the \
reply must read as natural Nigerian Pidgin, not standard English>,
  "reason": "<one short sentence>"
}

Score 1.0 only if the reply is warm, on-strategy, free of blame/threats, and in \
the right language. Penalise robotic, pushy, or off-language replies.
"""


def judge_reply(
    client: LLMClient,
    *,
    context: dict,
    expected_language: str,
    tone_notes: str,
    reply: str,
    conversation_id: str,
) -> dict:
    """Return {score, language_ok, reason}. Degrades to a neutral score on error."""
    payload = (
        f"Customer context: {context}\n"
        f"Expected language: {expected_language}\n"
        f"Expected tone: {tone_notes}\n\n"
        f"Agent reply:\n{reply or '(empty)'}"
    )
    try:
        parsed, _ = client.complete_json(
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": payload}],
            conversation_id=f"judge:{conversation_id}",
        )
    except LLMError as exc:
        return {"score": None, "language_ok": None, "reason": f"judge failed: {exc}"}

    return {
        "score": parsed.get("score"),
        "language_ok": parsed.get("language_ok"),
        "reason": parsed.get("reason", ""),
    }
