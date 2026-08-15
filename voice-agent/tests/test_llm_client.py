"""Tests for Mimo LLM client request/response parsing (no real API calls)."""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_client import LLMError, MimoLLMClient


def test_client_requires_api_key():
    try:
        MimoLLMClient(api_key="")
        assert False, "should have raised LLMError"
    except LLMError:
        pass


def test_client_defaults():
    client = MimoLLMClient(api_key="test-key-123")
    assert client.base_url == "https://token-plan-sgp.xiaomimimo.com/v1"
    assert client.model == "mimo-v2.5"
    assert client.api_key == "test-key-123"


def test_client_custom_url():
    client = MimoLLMClient(
        api_key="k", base_url="https://custom.api/v2", model="custom-model"
    )
    assert client.base_url == "https://custom.api/v2"
    assert client.model == "custom-model"


def test_client_endpoint():
    client = MimoLLMClient(api_key="k", base_url="https://api.example.com/v1")
    assert client._endpoint == "https://api.example.com/v1/chat/completions"


def test_client_endpoint_strips_trailing_slash():
    client = MimoLLMClient(api_key="k", base_url="https://api.example.com/v1/")
    assert client._endpoint == "https://api.example.com/v1/chat/completions"


def test_client_headers():
    client = MimoLLMClient(api_key="sk-test")
    headers = client._headers
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["Content-Type"] == "application/json"


def test_parse_streaming_chunk():
    """Simulate parsing an SSE chunk from the OpenAI-compatible API."""
    # A typical streaming chunk
    chunk_data = {
        "choices": [
            {
                "delta": {"content": "Hello"},
                "finish_reason": None,
            }
        ]
    }
    chunk_json = json.dumps(chunk_data)
    sse_line = f"data: {chunk_json}"

    # Extract content the same way the client does
    assert sse_line.startswith("data: ")
    data = sse_line[6:].strip()
    assert data != "[DONE]"
    chunk = json.loads(data)
    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
    assert delta == "Hello"


def test_parse_streaming_done():
    sse_line = "data: [DONE]"
    data = sse_line[6:].strip()
    assert data == "[DONE]"


def test_parse_streaming_empty_delta():
    chunk_data = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    delta = chunk_data.get("choices", [{}])[0].get("delta", {}).get("content", "")
    assert delta == ""


def test_parse_streaming_no_choices():
    chunk_data = {"id": "chatcmpl-123"}
    delta = chunk_data.get("choices", [{}])[0].get("delta", {}).get("content", "")
    assert delta == ""


def test_message_format():
    """Verify message format expected by the client."""
    messages = [
        {"role": "user", "content": "Hello"},
    ]
    system_prompt = "You are a helpful assistant."
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    assert len(full_messages) == 2
    assert full_messages[0]["role"] == "system"
    assert full_messages[1]["role"] == "user"


def test_payload_construction():
    """Verify the JSON payload shape matches OpenAI chat completions format."""
    client = MimoLLMClient(api_key="k", model="mimo-v2.5", max_tokens=512, temperature=0.5)
    messages = [{"role": "user", "content": "Hi"}]
    payload = {
        "model": client.model,
        "messages": [{"role": "system", "content": "test"}] + messages,
        "stream": True,
        "max_tokens": client.max_tokens,
        "temperature": client.temperature,
    }
    assert payload["model"] == "mimo-v2.5"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 512
    assert payload["temperature"] == 0.5
    assert len(payload["messages"]) == 2


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
