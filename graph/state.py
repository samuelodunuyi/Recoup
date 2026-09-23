"""Shared state for the recovery graph.

A single TypedDict flows through every node. LangGraph merges each node's returned
partial dict into this state, and we persist it to Postgres between turns so the
agent remembers prior promises and doesn't repeat itself (brief §2, "Memory").
"""

from __future__ import annotations

from typing import TypedDict


class RecoveryState(TypedDict, total=False):
    # ─── Identity / context (set by the caller per conversation) ───
    conversation_id: str
    run_id: str                # per-turn trace id
    customer_name: str
    decline_code: str          # e.g. "insufficient_funds"
    processor: str             # "paystack" | "flutterwave" | "mobile_money"
    language: str              # "english" | "pidgin"
    amount: float
    currency: str              # e.g. "NGN"
    # Declared so LangGraph carries them through (it drops undeclared keys).
    phone: str | None          # WhatsApp number, when the channel is live
    email: str | None          # needed by processors to create checkout links
    authorization_code: str | None  # Paystack saved card, for scheduled retries
    status: str                # "open" | "recovered"
    payment_link: str | None   # cached checkout URL for this failure

    # ─── Why this turn is running ───
    trigger: str | None        # None (customer spoke) | "payment_failed" | "scheduled_retry"

    # ─── This turn's input ───
    customer_message: str      # empty on the initial failed-payment event

    # ─── Memory carried across turns ───
    history: list[dict]        # [{"role": "customer"|"agent", "content": str}]
    promises: list[str]        # captured commitments, e.g. "will pay Friday"

    # ─── Working values produced by nodes ───
    route: str                 # classifier output (see actions.Route)
    strategy: str              # retrieved recovery strategy (Decline-Intelligence)
    strategy_sources: list[str]
    reply: str                 # text to send back to the customer
    action: dict               # structured action (see actions.py)
    link_sent: bool            # whether a payment link has already been sent this convo
