"""Routing states and structured actions the agent can emit.

The Negotiator returns one of these actions as JSON (not freeform text), so the
surrounding system can act on it deterministically — brief §2, point 3.
"""

from __future__ import annotations


class Route:
    """Classifier outputs — the conversational state of the customer."""

    NEW_FAILURE = "new_failure"          # first contact after a failed charge
    ALREADY_PAID = "already_paid"        # "I already paid"
    PAY_LATER = "pay_later"              # "I'll pay later / on payday"
    DISPUTE = "dispute"                  # "why was I charged?"
    NEEDS_HUMAN = "needs_human"          # out of scope / escalate

    ALL = {NEW_FAILURE, ALREADY_PAID, PAY_LATER, DISPUTE, NEEDS_HUMAN}


class Action:
    """Structured actions the Negotiator can emit."""

    SEND_PAYMENT_LINK = "SEND_PAYMENT_LINK"
    SCHEDULE_RETRY = "SCHEDULE_RETRY"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    NONE = "NONE"

    ALL = {SEND_PAYMENT_LINK, SCHEDULE_RETRY, ESCALATE_TO_HUMAN, NONE}


def empty_action() -> dict:
    return {"type": Action.NONE, "schedule_for": None, "promise": None}
