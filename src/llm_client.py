"""LLM client abstraction for ClearFeed.

Provides LLMClient with AnthropicBackend. A second backend can be added later
by implementing the Backend protocol and registering it in BACKENDS.

Usage:
    client = LLMClient()
    result = client.call("classify", prompt="...", system="...")
"""

import json
import logging
from typing import Protocol, runtime_checkable

import anthropic
import httpx

import config
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import security_config

from utils import retry

log = logging.getLogger(__name__)


@runtime_checkable
class Backend(Protocol):
    """Protocol for LLM backends."""

    def call(
        self, model_id: str, system: str, prompt: str, max_tokens: int, cacheable: bool = True
    ) -> str:
        """Send a prompt and return the text response."""
        ...


class AnthropicBackend:
    """Claude API backend via the official Anthropic SDK."""

    def __init__(self) -> None:
        self._client = anthropic.Anthropic(
            api_key=security_config.ANTHROPIC_API_KEY,
            timeout=httpx.Timeout(config.LLM_TIMEOUT_SECONDS, connect=5.0),
        )
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0
        self.last_model_id: str = ""

    @retry(
        exceptions=(
            anthropic.APIError,
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
        )
    )
    def call(
        self, model_id: str, system: str, prompt: str, max_tokens: int, cacheable: bool = True
    ) -> str:
        """Call the Anthropic Messages API via streaming. Returns the first text block.

        Streaming is used for all calls so large max_tokens (digests can run to tens
        of thousands of output tokens) don't hit the SDK's non-streaming HTTP timeout.

        cacheable=True marks the system block with cache_control so repeat calls
        sharing this exact system text within the 5-minute TTL read at ~10% of input
        cost instead of paying full price each time. Only worth it when the caller
        expects a follow-up call with the same system text soon (e.g. many records
        processed in one run against the same classify/summarize/action prompt) — set
        cacheable=False for one-shot calls (e.g. a digest compose that runs once per
        schedule cycle), where cache_control would only add the ~1.25x write premium
        with no read to offset it.
        """
<<<<<<< Updated upstream
=======
        system_param = (
            [
                {
                    "type": "text",
                    "text": system,
                    **({"cache_control": {"type": "ephemeral"}} if cacheable else {}),
                }
            ]
            if system
            else []
        )
>>>>>>> Stashed changes
        with self._client.messages.stream(
            model=model_id,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
        self.last_input_tokens = message.usage.input_tokens
        self.last_output_tokens = message.usage.output_tokens
        self.last_model_id = model_id
        if message.stop_reason == "max_tokens":
            log.warning(
                "LLM output hit max_tokens=%d for model=%s — response was truncated",
                max_tokens,
                model_id,
            )
        return message.content[0].text


# Registry: alias -> Backend class. Add OllamaBackend here in v2.
_BACKEND_REGISTRY: dict[str, type[Backend]] = {
    "anthropic": AnthropicBackend,
}
_DEFAULT_BACKEND = "anthropic"


class LLMClient:
    """Routes LLM calls to the configured backend using operation-based model aliases.

    Args:
        backend_key: which backend to use (default: "anthropic").
        max_tokens: override default output cap.
    """

    def __init__(
        self,
        backend_key: str = _DEFAULT_BACKEND,
        max_tokens: int = config.LLM_MAX_TOKENS,
    ) -> None:
        if backend_key not in _BACKEND_REGISTRY:
            raise ValueError(
                f"Unknown LLM backend: {backend_key!r}. Valid: {list(_BACKEND_REGISTRY)}"
            )
        self._backend: Backend = _BACKEND_REGISTRY[backend_key]()
        self._max_tokens = max_tokens
        self._accum_input: int = 0
        self._accum_output: int = 0
        self._last_model_id: str = ""

    def reset_usage(self) -> None:
        """Reset accumulated token counters. Call before a classify chain."""
        self._accum_input = 0
        self._accum_output = 0
        self._last_model_id = ""

    def consume_usage(self) -> tuple[str | None, int]:
        """Return (model_id, total_tokens) accumulated since last reset_usage().

        Reads from the backend's last-call attributes (set by AnthropicBackend.call).
        The hasattr guard keeps MagicMock backends working without changes.
        """
        if hasattr(self._backend, "last_model_id"):
            self._accum_input += getattr(self._backend, "last_input_tokens", 0)
            self._accum_output += getattr(self._backend, "last_output_tokens", 0)
            self._last_model_id = getattr(self._backend, "last_model_id", "") or self._last_model_id
        total = self._accum_input + self._accum_output
        return self._last_model_id or None, total

    def call(
        self,
        operation: str,
        prompt: str,
        system: str = "",
        model_override: str | None = None,
        max_tokens: int | None = None,
        cacheable: bool = True,
    ) -> str:
        """Call the LLM for the named operation.

        Args:
            operation: key into LLM_ROUTING (e.g. "classify", "digest").
            prompt: user-turn content.
            system: optional system prompt.
            model_override: alias ("haiku"/"sonnet") or full model ID; bypasses routing.
            max_tokens: overrides instance default.
            cacheable: whether to mark `system` with cache_control (see
                AnthropicBackend.call). Default True; pass False for calls known to
                run once per system-prompt text (e.g. a digest compose), where
                caching only adds cost with no read to offset it.

        Returns:
            Raw LLM text response.
        """
        alias = model_override or config.LLM_ROUTING.get(operation, "haiku")
        model_id = config.MODEL_IDS.get(
            alias, alias
        )  # fall through if already a full ID
        tokens = max_tokens or self._max_tokens
        log.debug("LLM call: operation=%s model=%s", operation, model_id)
        return self._backend.call(model_id, system, prompt, tokens, cacheable)

    def call_json(
        self,
        operation: str,
        prompt: str,
        system: str = "",
        model_override: str | None = None,
        max_tokens: int | None = None,
        json_retries: int = 2,
        cacheable: bool = True,
    ) -> dict:
        """Like call(), but parses and returns JSON.

        On parse failure, retries with the actual parse error appended to the
        prompt so the model can see and fix what broke (commonly an unescaped
        quote or raw control character inside a string value) — a plain resend
        of the identical prompt relies on sampling luck alone.

        Raises:
            ValueError: if the response cannot be parsed as JSON after retries.
        """
        current_prompt = prompt
        last_exc: ValueError | None = None
        for attempt in range(1 + json_retries):
            raw = self.call(
                operation, current_prompt, system, model_override, max_tokens, cacheable
            )
            try:
                return _parse_json(raw)
            except ValueError as exc:
                last_exc = exc
                log.warning(
                    "JSON parse failed (attempt %d/%d): %s",
                    attempt + 1,
                    1 + json_retries,
                    exc,
                )
                current_prompt = (
                    f"{prompt}\n\n---\n\nYour previous response could not be parsed as "
                    f"JSON:\n{exc}\n\nReturn ONLY the corrected response as strictly "
                    f"valid JSON — no surrounding text or markdown fences. Every "
                    f'double-quote character inside a string value must be escaped '
                    f'as \\", and every literal newline inside a string value must '
                    f'be escaped as \\n.'
                )
        raise last_exc


def _parse_json(text: str) -> dict:
    """Extract and parse a JSON object from LLM output (handles markdown fences)."""
    stripped = text.strip()
    # strip ```json ... ``` fences if present
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        context_start = max(0, exc.pos - 200)
        context_end = min(len(stripped), exc.pos + 200)
        raise ValueError(
            f"Cannot parse LLM output as JSON: {exc}\n"
            f"Context: ...{stripped[context_start:context_end]}..."
        ) from exc
