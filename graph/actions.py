"""Routing states and structured actions the agent can emit.

The Negotiator returns one of these actions as JSON (not freeform text), so the
surrounding system can act on it deterministically — brief §2, point 3.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, field_validator


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
    return {"type": Action.NONE, "schedule_for": None, "promise": None, "retry_at": None}


# Furthest ahead a retry may be scheduled; anything beyond is treated as unparsed.
MAX_RETRY_DAYS = 60


class ActionModel(BaseModel):
    """Schema-validates the Negotiator's structured action (#16). Unknown fields are
    dropped; an out-of-set type becomes NONE; non-string fields are coerced."""

    model_config = ConfigDict(extra="ignore")

    type: str = Action.NONE
    schedule_for: str | None = None
    promise: str | None = None
    retry_at: str | None = None  # ISO date (YYYY-MM-DD) for SCHEDULE_RETRY

    @field_validator("type", mode="before")
    @classmethod
    def _valid_type(cls, v: object) -> str:
        return v if v in Action.ALL else Action.NONE

    @field_validator("retry_at", mode="before")
    @classmethod
    def _valid_date(cls, v: object) -> str | None:
        """Keep only a real date between today and MAX_RETRY_DAYS ahead."""
        try:
            d = date.fromisoformat(str(v)[:10])
        except (TypeError, ValueError):
            return None
        today = date.today()
        return d.isoformat() if today <= d <= today + timedelta(days=MAX_RETRY_DAYS) else None

    @field_validator("schedule_for", "promise", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str | None:
        return None if v is None else str(v)


def validate_action(raw: object) -> dict:
    """Coerce whatever the model produced into a valid action dict."""
    if isinstance(raw, str):
        raw = {"type": raw}
    elif not isinstance(raw, dict):
        raw = {}
    try:
        return ActionModel(**raw).model_dump()
    except Exception:
        return empty_action()
