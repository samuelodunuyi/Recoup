"""Provider-agnostic LLM client with fallback routing and token/cost logging.

One interface (`LLMClient.complete`), two backends (Anthropic, OpenAI). The client
tries the primary provider and, on error or timeout, automatically retries on the
secondary. Every call emits a structured JSON log line with provider, model, token
counts, latency, and cost, and is accumulated into a per-conversation cost report.

This maps directly to the brief's section 4: vendor-agnostic by default, fallback
routing as standard practice, and token/cost governance.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Protocol

import anthropic
import openai

from app.config import Settings, get_settings
from app.context import request_id_var

logger = logging.getLogger("recoup.llm")


# ─── Pricing ────────────────────────────────────────────────────────────────
# USD per 1,000,000 tokens, as (input, output). Used purely for cost logging.
# Keep this in sync with provider pricing; unknown models log a warning and $0.
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic (current sheet)
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = PRICING.get(model)
    if rates is None:
        logger.warning("no pricing for model %r; cost logged as 0", model)
        return 0.0
    input_rate, output_rate = rates
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000


# ─── Normalised result ──────────────────────────────────────────────────────
@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cost_usd: float
    stop_reason: str = ""  # "end_turn"/"max_tokens"/"stop"/"length" — used to detect truncation


class LLMError(RuntimeError):
    """Raised when every configured provider fails."""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerating prose/fences."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError(f"could not parse JSON from model reply: {exc}") from exc
    raise LLMError(f"model reply contained no JSON object: {text[:200]!r}")


# ─── Provider backends ──────────────────────────────────────────────────────
class Provider(Protocol):
    name: str

    def generate(
        self, system: str, messages: list[dict], max_tokens: int, thinking: bool
    ) -> LLMResult: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings):
        self._model = settings.anthropic_model
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
        )
        # Older anthropic SDKs don't accept the `thinking` kwarg; detect support so
        # we never pass an argument the installed SDK can't handle.
        try:
            params = inspect.signature(self._client.messages.create).parameters
            self._supports_thinking = "thinking" in params
        except (TypeError, ValueError):
            self._supports_thinking = False

    def generate(
        self, system: str, messages: list[dict], max_tokens: int, thinking: bool = True
    ) -> LLMResult:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if self._supports_thinking:
            # Disable thinking for short structured-JSON calls so reasoning tokens
            # can't eat the output budget and truncate the JSON.
            kwargs["thinking"] = {"type": "adaptive"} if thinking else {"type": "disabled"}

        start = time.perf_counter()
        response = self._client.messages.create(**kwargs)
        latency_ms = (time.perf_counter() - start) * 1000

        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens
        return LLMResult(
            text=text,
            provider=self.name,
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=_cost_usd(self._model, prompt_tokens, completion_tokens),
            stop_reason=response.stop_reason or "",
        )


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings):
        self._model = settings.openai_model
        self._client = openai.OpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout_seconds,
        )

    def generate(
        self, system: str, messages: list[dict], max_tokens: int, thinking: bool = True
    ) -> LLMResult:
        # `thinking` is Anthropic-specific; OpenAI ignores it. Kept in the signature
        # so the client can call any provider uniformly.
        # OpenAI carries the system prompt as the first message in the list.
        full_messages = [{"role": "system", "content": system}, *messages]
        start = time.perf_counter()
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=full_messages,
        )
        latency_ms = (time.perf_counter() - start) * 1000

        text = response.choices[0].message.content or ""
        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens
        return LLMResult(
            text=text,
            provider=self.name,
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=_cost_usd(self._model, prompt_tokens, completion_tokens),
            stop_reason=response.choices[0].finish_reason or "",
        )


_PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}

_default_client: "LLMClient | None" = None


def get_client() -> "LLMClient":
    """Process-wide shared client, so cost accounting aggregates across the app
    and the graph nodes (which must use the *same* instance)."""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client

# Provider SDK errors that should trigger a fallback rather than crash the call.
_FALLBACK_ERRORS = (anthropic.APIError, openai.APIError, TimeoutError)

# Transient errors worth retrying on the SAME provider before falling over (#5).
_RETRYABLE_ERRORS = (
    anthropic.RateLimitError, anthropic.APITimeoutError,
    anthropic.InternalServerError, anthropic.APIConnectionError,
    openai.RateLimitError, openai.APITimeoutError,
    openai.InternalServerError, openai.APIConnectionError,
    TimeoutError,
)


# ─── Client ─────────────────────────────────────────────────────────────────
# Cap on conversations held in the in-process cost report (LRU-evicted past this).
_MAX_TRACKED_CONVERSATIONS = 10_000


@dataclass
class _ConversationCost:
    total_cost_usd: float = 0.0
    by_provider: dict[str, float] = field(default_factory=dict)
    calls: int = 0


class LLMClient:
    """Routes completions across providers with fallback and cost accounting."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        # Ordered list of providers to try: primary first, then fallback.
        order = [self._settings.llm_primary, self._settings.llm_fallback]
        self._providers: list[Provider] = []
        for name in order:
            cls = _PROVIDER_CLASSES.get(name)
            if cls is None:
                raise ValueError(f"unknown provider {name!r}")
            self._providers.append(cls(self._settings))
        self._costs: dict[str, _ConversationCost] = {}
        # (timestamp, cost) of recent calls, for the global rolling-24h budget when
        # no database is configured.
        self._recent: deque[tuple[float, float]] = deque()
        # FastAPI runs sync endpoints in a threadpool, so the shared cost report can
        # be mutated concurrently — guard it.
        self._lock = threading.Lock()
        # Optional persistence hook (set by the app) receiving per-call metadata.
        self._sink = None

    def set_sink(self, sink) -> None:
        """Register a callable(record: dict) to persist each call's metadata."""
        self._sink = sink

    def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        conversation_id: str = "default",
        thinking: bool = True,
    ) -> LLMResult:
        """Generate a reply. Retries transient errors per provider (with backoff),
        then falls over to the next provider; raises only if all fail."""
        max_tokens = max_tokens or self._settings.llm_max_tokens
        max_retries = self._settings.llm_max_retries
        errors: list[str] = []

        for provider in self._providers:
            for attempt in range(max_retries + 1):
                try:
                    result = provider.generate(system, messages, max_tokens, thinking)
                except _RETRYABLE_ERRORS as exc:
                    if attempt < max_retries:
                        backoff = 0.5 * (2 ** attempt)
                        logger.warning("provider %s transient error (retry %d in %.1fs): %s",
                                       provider.name, attempt + 1, backoff, exc)
                        time.sleep(backoff)
                        continue
                    errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
                    break  # exhausted retries — fall over to next provider
                except _FALLBACK_ERRORS as exc:  # non-transient — fall over immediately
                    errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
                    logger.warning("provider %s failed, trying fallback: %s", provider.name, exc)
                    break
                else:
                    self._record(conversation_id, result)
                    return result

        raise LLMError(
            f"all providers failed for conversation {conversation_id!r}: "
            + "; ".join(errors)
        )

    def _record(self, conversation_id: str, result: LLMResult) -> None:
        """Log call metadata as structured JSON and accumulate cost by conversation.

        The model's generated text is deliberately NOT logged — it can contain
        customer PII. Only token counts, latency, and cost are recorded.
        """
        rec = {
            "conversation_id": conversation_id,
            "provider": result.provider,
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "latency_ms": round(result.latency_ms, 1),
            "cost_usd": result.cost_usd,
            "request_id": request_id_var.get(),
        }
        logger.info(json.dumps(rec))
        # #18 alert on an unexpectedly expensive single call.
        if result.cost_usd > self._settings.max_cost_per_call:
            logger.warning("call cost $%.4f exceeds per-call cap $%.4f (%s/%s)",
                           result.cost_usd, self._settings.max_cost_per_call,
                           result.provider, result.model)
        if self._sink is not None:
            try:
                self._sink(rec)
            except Exception as exc:  # persistence must not break the call path
                logger.warning("cost sink failed: %s", exc)

        with self._lock:
            # Re-insert on every call so dict order is least-recently-used first,
            # then evict the oldest so the report can't grow without bound.
            bucket = self._costs.pop(conversation_id, None) or _ConversationCost()
            self._costs[conversation_id] = bucket
            if len(self._costs) > _MAX_TRACKED_CONVERSATIONS:
                del self._costs[next(iter(self._costs))]
            self._recent.append((time.time(), result.cost_usd))
            bucket.total_cost_usd += result.cost_usd
            bucket.by_provider[result.provider] = (
                bucket.by_provider.get(result.provider, 0.0) + result.cost_usd
            )
            bucket.calls += 1

    def complete_json(
        self,
        system: str,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        conversation_id: str = "default",
    ) -> tuple[dict, LLMResult]:
        """Complete and parse the reply as a JSON object.

        Prompt-based JSON (rather than each provider's native structured-output
        format) keeps the path identical across Anthropic and OpenAI, so fallback
        stays uniform. Returns (parsed_dict, raw_result).
        """
        sys_json = system + "\n\nRespond ONLY with a single valid JSON object."
        base_tokens = max_tokens or self._settings.llm_max_tokens
        result = self.complete(
            system=sys_json, messages=messages, max_tokens=base_tokens,
            conversation_id=conversation_id, thinking=False,
        )
        try:
            return _extract_json(result.text), result
        except LLMError:
            # #6: if the reply was truncated (hit the token cap), retry once with a
            # bigger budget before giving up.
            if result.stop_reason in ("max_tokens", "length"):
                logger.warning("JSON reply truncated (%s); retrying with more tokens",
                               result.stop_reason)
                result = self.complete(
                    system=sys_json, messages=messages, max_tokens=base_tokens * 3,
                    conversation_id=conversation_id, thinking=False,
                )
                return _extract_json(result.text), result
            raise

    def cost_last_24h(self) -> float:
        """In-process spend across all conversations over the last 24 hours."""
        cutoff = time.time() - 24 * 3600
        with self._lock:
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.popleft()
            return sum(cost for _, cost in self._recent)

    def cost_report(self, conversation_id: str | None = None) -> dict:
        """Total cost per conversation and per provider (brief §4's tiny report)."""
        with self._lock:
            if conversation_id is not None:
                bucket = self._costs.get(conversation_id, _ConversationCost())
                return {"conversation_id": conversation_id, **asdict(bucket)}
            return {cid: asdict(bucket) for cid, bucket in self._costs.items()}
