"""Pure scoring functions: compare expected vs. actual graph output."""

from __future__ import annotations


def score_route(expected: str, actual: str | None) -> bool:
    return expected == actual


def score_action(expected: str, actual: dict | None) -> bool:
    actual_type = (actual or {}).get("type")
    return expected == actual_type
