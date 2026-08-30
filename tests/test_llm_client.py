"""Tests for llm_client.AnthropicBackend response parsing.

Covers the ThinkingBlock regression (2026-07-27): Sonnet 5 can prepend a
ThinkingBlock (no `.text` attribute) ahead of the text block, and can even
exhaust max_tokens on thinking alone before emitting any text. The backend
must find the text block by type rather than assuming content[0], and must
raise a clear error (not an AttributeError) when no text block exists at all.
"""

import sys
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from llm_client import AnthropicBackend  # noqa: E402


class _FakeBlock:
    """Mimics an Anthropic content block — only the block type in use has a payload
    attribute, matching how ThinkingBlock (`.thinking`) vs TextBlock (`.text`) differ."""

    def __init__(self, type_: str, **payload):
        self.type = type_
        for key, value in payload.items():
            setattr(self, key, value)


def _fake_usage(input_tokens=100, output_tokens=50, cache_read=0, cache_creation=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


def _backend_with_stream(final_message):
    """Build an AnthropicBackend whose messages.stream(...) returns final_message."""
    backend = AnthropicBackend()
    stream_ctx = MagicMock()
    stream_ctx.__enter__.return_value.get_final_message.return_value = final_message
    backend._client.messages.stream = MagicMock(return_value=stream_ctx)
    return backend


def test_call_finds_text_block_after_leading_thinking_block():
    """A ThinkingBlock ahead of the TextBlock must not break extraction."""
    message = SimpleNamespace(
        content=[
            _FakeBlock("thinking", thinking="reasoning about the classification..."),
            _FakeBlock("text", text='{"tags": ["actionable"]}'),
        ],
        usage=_fake_usage(),
        stop_reason="end_turn",
    )
    backend = _backend_with_stream(message)

    result = backend.call("claude-sonnet-5", "system", "user", 16000)

    assert result == '{"tags": ["actionable"]}'


def test_call_raises_clear_error_when_no_text_block_present():
    """Thinking-only content (budget exhausted before any text) must raise a
    diagnosable error, not AttributeError: 'ThinkingBlock' object has no attribute 'text'."""
    message = SimpleNamespace(
        content=[_FakeBlock("thinking", thinking="ran out of budget mid-reasoning")],
        usage=_fake_usage(),
        stop_reason="max_tokens",
    )
    backend = _backend_with_stream(message)

    with pytest.raises(ValueError, match="No text block in response content"):
        backend.call("claude-sonnet-5", "system", "user", 16000)


def test_call_returns_text_when_only_block_is_text():
    """Baseline: a plain text-only response (no thinking) still works."""
    message = SimpleNamespace(
        content=[_FakeBlock("text", text='{"ok": true}')],
        usage=_fake_usage(),
        stop_reason="end_turn",
    )
    backend = _backend_with_stream(message)

    result = backend.call("claude-haiku-4-5-20251001", "system", "user", 16000)

    assert result == '{"ok": true}'


# ---------------------------------------------------------------------------
# cacheable flag — controls whether the system block gets cache_control
# ---------------------------------------------------------------------------


def _basic_message():
    return SimpleNamespace(
        content=[_FakeBlock("text", text="ok")],
        usage=_fake_usage(),
        stop_reason="end_turn",
    )


def test_call_marks_system_cacheable_by_default():
    """Default cacheable=True must attach cache_control to the system block."""
    backend = _backend_with_stream(_basic_message())

    backend.call("claude-haiku-4-5-20251001", "static system prompt", "user", 16000)

    sent_system = backend._client.messages.stream.call_args.kwargs["system"]
    assert sent_system == [
        {
            "type": "text",
            "text": "static system prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_call_omits_cache_control_when_not_cacheable():
    """cacheable=False must send the system text with no cache_control marker —
    for one-shot calls (e.g. a digest compose), cache_control only adds the
    write premium with no follow-up read to pay it back."""
    backend = _backend_with_stream(_basic_message())

    backend.call(
        "claude-haiku-4-5-20251001", "static system prompt", "user", 16000, cacheable=False
    )

    sent_system = backend._client.messages.stream.call_args.kwargs["system"]
    assert sent_system == [{"type": "text", "text": "static system prompt"}]


def test_call_omits_system_block_entirely_when_system_empty():
    """Empty system must produce an empty list regardless of cacheable."""
    backend = _backend_with_stream(_basic_message())

    backend.call("claude-haiku-4-5-20251001", "", "user", 16000)

    assert backend._client.messages.stream.call_args.kwargs["system"] == []
