"""Tests for the provider-agnostic LLM client: fallback, cost, JSON, thinking."""

from __future__ import annotations

import anthropic
import pytest

from llm.client import LLMClient, LLMError, LLMResult, _extract_json


class _StubProvider:
    """A provider that returns a canned result or raises a chosen exception."""

    def __init__(self, name, *, result=None, raises=None):
        self.name = name
        self._result = result
        self._raises = raises
        self.calls = []

    def generate(self, system, messages, max_tokens, thinking=True):
        self.calls.append({"thinking": thinking})
        if self._raises is not None:
            raise self._raises
        return self._result


def _result(provider="anthropic", cost=0.001):
    return LLMResult("ok", provider, "m", 1, 1, 1.0, cost)


def _client_with(providers):
    import threading

    c = LLMClient.__new__(LLMClient)          # bypass __init__ (no real SDKs)
    from app.config import get_settings
    c._settings = get_settings()
    c._providers = providers
    c._costs = {}
    c._recent = __import__("collections").deque()
    c._lock = threading.Lock()
    c._sink = None
    return c


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_with_prose():
    assert _extract_json('Sure!\n```json\n{"route": "pay_later"}\n```') == {"route": "pay_later"}


def test_extract_json_raises_when_absent():
    with pytest.raises(LLMError):
        _extract_json("no json here")


def test_fallback_used_when_primary_fails():
    primary = _StubProvider("anthropic", raises=anthropic.APIConnectionError(request=None))
    secondary = _StubProvider("openai", result=_result("openai"))
    client = _client_with([primary, secondary])
    client._settings.llm_max_retries = 0  # test pure fallover, not retry

    res = client.complete("sys", [{"role": "user", "content": "hi"}], conversation_id="c")

    assert res.provider == "openai"
    assert len(primary.calls) == 1 and len(secondary.calls) == 1


def test_all_providers_failing_raises_llmerror():
    p1 = _StubProvider("anthropic", raises=anthropic.APIConnectionError(request=None))
    p2 = _StubProvider("openai", raises=TimeoutError("slow"))
    client = _client_with([p1, p2])
    client._settings.llm_max_retries = 0

    with pytest.raises(LLMError):
        client.complete("sys", [{"role": "user", "content": "hi"}])


def test_cost_report_aggregates_by_conversation_and_provider():
    client = _client_with([_StubProvider("anthropic", result=_result("anthropic", 0.002))])
    client.complete("s", [{"role": "user", "content": "x"}], conversation_id="conv")
    client.complete("s", [{"role": "user", "content": "y"}], conversation_id="conv")

    report = client.cost_report("conv")
    assert report["calls"] == 2
    assert round(report["total_cost_usd"], 6) == 0.004
    assert round(report["by_provider"]["anthropic"], 6) == 0.004


class _FlakyProvider:
    """Fails with a retryable error N times, then returns a result."""

    def __init__(self, name, fail_times, result):
        self.name = name
        self._fail_times = fail_times
        self._result = result
        self.calls = 0

    def generate(self, system, messages, max_tokens, thinking=True):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise TimeoutError("slow down")  # retryable
        return self._result


def test_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)  # no real backoff wait
    provider = _FlakyProvider("openai", fail_times=2, result=_result("openai"))
    client = _client_with([provider])
    client._settings.llm_max_retries = 2

    res = client.complete("s", [{"role": "user", "content": "hi"}])
    assert res.provider == "openai"
    assert provider.calls == 3  # 2 failures + 1 success


def test_exhausted_retries_fall_over_to_next_provider(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    primary = _FlakyProvider("anthropic", fail_times=99, result=_result())
    secondary = _StubProvider("openai", result=_result("openai"))
    client = _client_with([primary, secondary])
    client._settings.llm_max_retries = 1

    res = client.complete("s", [{"role": "user", "content": "hi"}])
    assert res.provider == "openai"
    assert primary.calls == 2  # initial + 1 retry, then fell over


def test_complete_json_retries_on_truncation(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    truncated = LLMResult("{\"reply\": \"hel", "anthropic", "m", 1, 1, 1.0, 0.0, stop_reason="max_tokens")
    full = LLMResult('{"reply": "hello"}', "anthropic", "m", 1, 2, 1.0, 0.0, stop_reason="end_turn")
    provider = _StubProvider("anthropic")
    seq = [truncated, full]
    provider.generate = lambda *a, **k: seq.pop(0)  # type: ignore
    client = _client_with([provider])

    parsed, _ = client.complete_json("s", [{"role": "user", "content": "hi"}])
    assert parsed == {"reply": "hello"}


def test_complete_json_disables_thinking():
    provider = _StubProvider("anthropic", result=_result())
    provider._result = LLMResult('{"ok": true}', "anthropic", "m", 1, 1, 1.0, 0.0)
    client = _client_with([provider])

    parsed, _ = client.complete_json("sys", [{"role": "user", "content": "hi"}])

    assert parsed == {"ok": True}
    assert provider.calls[0]["thinking"] is False  # structured calls must not think
