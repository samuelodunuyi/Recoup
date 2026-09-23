"""Eval runner: execute every scenario through the graph and score it.

Measures routing accuracy, action correctness, latency, cost (from the LLM client's
token logging), and tone/language via an LLM judge. Writes a markdown scorecard and
a JSON dump so before/after prompt changes can be compared.

Run:  python -m eval.runner            # full run with judge
      python -m eval.runner --no-judge # skip the tone judge (faster, cheaper)
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from eval import judge as judge_mod
from eval import scorer
from graph import run_turn
from graph.nodes import _client as agent_client
from llm import LLMClient

EVAL_DIR = Path(__file__).parent
SCENARIOS_PATH = EVAL_DIR / "scenarios.json"
SCORECARD_PATH = EVAL_DIR / "scorecard.md"
LAST_RUN_PATH = EVAL_DIR / "last_run.json"


def _build_state(scenario: dict) -> dict:
    context = dict(scenario["context"])
    promises = context.pop("promises", [])
    conv_id = f"eval:{scenario['id']}"
    return {
        "conversation_id": conv_id,
        **context,
        "history": list(scenario.get("history", [])),
        "promises": list(promises),
        "customer_message": scenario.get("message", ""),
    }


def run(use_judge: bool = True) -> dict:
    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    judge_client = LLMClient() if use_judge else None

    results = []
    for scenario in scenarios:
        conv_id = f"eval:{scenario['id']}"
        state = _build_state(scenario)

        start = time.perf_counter()
        out = run_turn(state)
        latency_ms = (time.perf_counter() - start) * 1000

        expected = scenario["expected"]
        route_ok = scorer.score_route(expected["route"], out.get("route"))
        action_ok = scorer.score_action(expected["action"], out.get("action"))
        cost = agent_client.cost_report(conv_id).get("total_cost_usd", 0.0)

        tone = {"score": None, "language_ok": None, "reason": "judge skipped"}
        if judge_client is not None:
            tone = judge_mod.judge_reply(
                judge_client,
                context=scenario["context"],
                expected_language=expected.get("language", "english"),
                tone_notes=expected.get("tone_notes", ""),
                reply=out.get("reply", ""),
                conversation_id=conv_id,
            )

        results.append(
            {
                "id": scenario["id"],
                "expected_route": expected["route"],
                "actual_route": out.get("route"),
                "route_ok": route_ok,
                "expected_action": expected["action"],
                "actual_action": (out.get("action") or {}).get("type"),
                "action_ok": action_ok,
                "latency_ms": round(latency_ms, 1),
                "cost_usd": cost,
                "tone_score": tone.get("score"),
                "language_ok": tone.get("language_ok"),
                "reply": out.get("reply", ""),
            }
        )

    summary = _summarise(results)
    _write_scorecard(summary, results)
    LAST_RUN_PATH.write_text(
        json.dumps({"summary": summary, "results": results}, indent=2),
        encoding="utf-8",
    )
    return {"summary": summary, "results": results}


def _summarise(results: list[dict]) -> dict:
    n = len(results)
    tone_scores = [r["tone_score"] for r in results if isinstance(r["tone_score"], (int, float))]
    lang_flags = [r["language_ok"] for r in results if isinstance(r["language_ok"], bool)]
    return {
        "scenarios": n,
        "routing_accuracy": round(sum(r["route_ok"] for r in results) / n, 3),
        "action_accuracy": round(sum(r["action_ok"] for r in results) / n, 3),
        "avg_latency_ms": round(statistics.mean(r["latency_ms"] for r in results), 1),
        "total_cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        "avg_cost_usd": round(sum(r["cost_usd"] for r in results) / n, 6),
        "avg_tone_score": round(statistics.mean(tone_scores), 3) if tone_scores else None,
        "language_pass_rate": round(sum(lang_flags) / len(lang_flags), 3) if lang_flags else None,
    }


def _write_scorecard(summary: dict, results: list[dict]) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Recoup eval scorecard",
        "",
        f"_Run: {ts} · {summary['scenarios']} scenarios_",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Routing accuracy | {summary['routing_accuracy']:.0%} |",
        f"| Action correctness | {summary['action_accuracy']:.0%} |",
        f"| Avg latency | {summary['avg_latency_ms']} ms |",
        f"| Total cost | ${summary['total_cost_usd']:.4f} |",
        f"| Avg cost / conversation | ${summary['avg_cost_usd']:.4f} |",
        f"| Avg tone score | {summary['avg_tone_score']} |",
        f"| Language pass rate | {summary['language_pass_rate']} |",
        "",
        "## Per-scenario",
        "",
        "| Scenario | Route (exp→act) | ✓ | Action (exp→act) | ✓ | Tone | Lang |",
        "| --- | --- | :-: | --- | :-: | :-: | :-: |",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} "
            f"| {r['expected_route']}→{r['actual_route']} | {'✅' if r['route_ok'] else '❌'} "
            f"| {r['expected_action']}→{r['actual_action']} | {'✅' if r['action_ok'] else '❌'} "
            f"| {r['tone_score']} | {'✅' if r['language_ok'] else ('❌' if r['language_ok'] is False else '–')} |"
        )
    SCORECARD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Recoup eval harness")
    parser.add_argument("--no-judge", action="store_true", help="skip the tone judge")
    args = parser.parse_args()

    out = run(use_judge=not args.no_judge)
    s = out["summary"]
    print(
        f"routing={s['routing_accuracy']:.0%} action={s['action_accuracy']:.0%} "
        f"avg_latency={s['avg_latency_ms']}ms avg_cost=${s['avg_cost_usd']:.4f} "
        f"tone={s['avg_tone_score']}"
    )
    # ASCII arrow: Windows consoles (cp1252) can't encode "→" and would crash here.
    print(f"scorecard -> {SCORECARD_PATH}")
