from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

_log = logging.getLogger(__name__)

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from loop1.config import config

_GROQ_BASE = "https://api.groq.com/openai/v1"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_CEREBRAS_BASE = "https://api.cerebras.ai/v1"
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_RETRYABLE_STATUS = {500, 502, 503, 504}  # 429 handled separately via fallback

# When a provider hits 429, try these fallbacks in order
_FALLBACK_CHAIN: dict[str, list[str]] = {
    "groq": [
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter/google/gemma-4-31b-it:free",
    ],
    "gemini": ["openrouter/nvidia/nemotron-3-super-120b-a12b:free"],
    "openrouter": [
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter/google/gemma-4-31b-it:free",
        "groq/qwen/qwen3.6-27b",  # last resort — capped at 8000 TPM on groq free tier
    ],
    "cerebras": ["openrouter/nvidia/nemotron-3-super-120b-a12b:free"],
}


def _groq_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY environment variable not set")
    return key


def _openrouter_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise _RateLimitError("OPENROUTER_API_KEY not set — skipping openrouter model")
    return key


def _cerebras_api_key() -> str:
    key = os.environ.get("CEREBRAS_API_KEY", "")
    if not key:
        raise _RateLimitError("CEREBRAS_API_KEY not set — skipping cerebras model")
    return key


def _gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise _RateLimitError("GEMINI_API_KEY not set — skipping gemini model")
    return key


def _is_retryable(exc: BaseException) -> bool:
    import httpx
    return isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, _RetryableHTTPError))


class _RetryableHTTPError(Exception):
    pass


class _RateLimitError(Exception):
    """Raised on HTTP 429 — triggers provider fallback, not same-provider retry."""
    pass


def _provider_prefix(model: str) -> str:
    if "/" in model:
        return model.split("/")[0]
    return "groq"  # default


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _call_llm_raw(model: str, messages: list[dict[str, str]], **kwargs: Any) -> tuple[str, int]:
    import httpx  # lazy import — avoids SSL/Keychain hang at server startup on macOS
    defaults: dict[str, Any] = {
        "temperature": config["thresholds"]["llm_temperature"],
        "max_tokens": config["thresholds"]["llm_max_tokens"],
    }
    defaults.update(kwargs)

    if model.startswith("gemini/"):
        base_url = _GEMINI_BASE
        api_key = _gemini_api_key()
        model_name = model.removeprefix("gemini/")
    elif model.startswith("openrouter/"):
        base_url = _OPENROUTER_BASE
        api_key = _openrouter_api_key()
        model_name = model.removeprefix("openrouter/")
    elif model.startswith("cerebras/"):
        key = os.environ.get("CEREBRAS_API_KEY", "")
        if not key:
            raise _RateLimitError("CEREBRAS_API_KEY not set — skipping cerebras model")
        base_url = _CEREBRAS_BASE
        api_key = key
        model_name = model.removeprefix("cerebras/")
        # Reasoning models burn tokens on CoT before output — require generous headroom
        defaults.setdefault("max_tokens", 8192)
        if defaults.get("max_tokens", 0) < 8192:
            defaults["max_tokens"] = 8192
    else:
        base_url = _GROQ_BASE
        api_key = _groq_api_key()
        model_name = model.removeprefix("groq/")

    payload = {
        "model": model_name,
        "messages": messages,
        **defaults,
    }

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code == 429:
        # Surface reset time so caller can wait the right amount
        reset_tokens = response.headers.get("x-ratelimit-reset-tokens", "")
        reset_requests = response.headers.get("x-ratelimit-reset-requests", "")
        raise _RateLimitError(f"HTTP 429 reset-tokens={reset_tokens} reset-requests={reset_requests}")
    if response.status_code in _RETRYABLE_STATUS:
        raise _RetryableHTTPError(f"HTTP {response.status_code}: {response.text[:200]}")
    if response.status_code != 200:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:400]}")

    data = response.json()
    choices = data.get("choices")
    if not choices:
        # Some free-tier/OpenRouter models occasionally return HTTP 200 with
        # a malformed or content-filtered body that has no "choices" at all
        # (rather than a proper error status) — same retry-then-fall-through
        # treatment as the empty-content case below, instead of a raw
        # KeyError leaking all the way up to the caller.
        raise _RetryableHTTPError(f"No 'choices' in response: {response.text[:300]}")
    msg = choices[0]["message"]
    # Reasoning models (e.g. gpt-oss-120b) may return content=None with reasoning in a
    # separate field when max_tokens is too low to finish the thinking phase.
    content: str = msg.get("content") or msg.get("reasoning") or ""
    if not content:
        raise _RetryableHTTPError("Empty content and reasoning in response — likely max_tokens too low")
    prompt_tokens: int = data.get("usage", {}).get("prompt_tokens", 0)
    return content, prompt_tokens


def _parse_reset_seconds(msg: str) -> float:
    """Parse reset time from Groq 429 message, e.g. 'reset-tokens=4.5s'."""
    import re
    # Find the shortest reset time across tokens and requests
    matches = re.findall(r"reset-(?:tokens|requests)=([\d.]+)([smh]?)", msg)
    if not matches:
        return 5.0
    seconds = []
    for val, unit in matches:
        v = float(val)
        if unit == "m":
            v *= 60
        elif unit == "h":
            v *= 3600
        seconds.append(v)
    return min(seconds) + 0.5  # small buffer


def _call_with_fallback(model: str, messages: list[dict[str, str]], **kwargs: Any) -> tuple[str, int]:
    """Try model then walk fallbacks on 429 or exhausted same-model retries."""
    prefix = _provider_prefix(model)
    all_models = [model] + _FALLBACK_CHAIN.get(prefix, [])
    last_err: Exception | None = None
    for m in all_models:
        try:
            return _call_llm_raw(m, messages, **kwargs)
        except _RateLimitError as e:
            last_err = e
            _log.warning("429 on %s — trying next fallback. (%s)", m, str(e)[:80])
            continue
        except _RetryableHTTPError as e:
            # _call_llm_raw already retried this model 3x internally and gave up —
            # a persistently overloaded/erroring provider shouldn't fail the whole
            # turn when other models in the chain are healthy.
            last_err = e
            _log.warning("%s exhausted retries — trying next fallback. (%s)", m, str(e)[:80])
            continue
    raise RuntimeError(
        f"All models are currently rate-limited or unavailable. Please wait a moment and try again. "
        f"(last error: {str(last_err)[:200]})"
    )


def call_llm(model: str, messages: list[dict[str, str]], **kwargs: Any) -> str:
    content, _ = _call_with_fallback(model, messages, **kwargs)
    return content


def call_llm_with_usage(
    model: str, messages: list[dict[str, str]], **kwargs: Any
) -> tuple[str, int]:
    return _call_with_fallback(model, messages, **kwargs)


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def call_llm_json(
    model: str,
    messages: list[dict[str, str]],
    max_parse_retries: int = 3,
    **kwargs: Any,
) -> dict[str, Any]:
    working_messages = list(messages)
    last_err: Exception | None = None

    for _ in range(max_parse_retries):
        raw = call_llm(model=model, messages=working_messages, **kwargs)
        cleaned = _strip_fences(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_err = exc
            working_messages = working_messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Your response was not valid JSON. Error: {exc}. "
                        "Reply with valid JSON only — no markdown fences, no prose."
                    ),
                },
            ]

    raise ValueError(
        f"Failed to get valid JSON after {max_parse_retries} attempts. "
        f"Last error: {last_err}"
    )
