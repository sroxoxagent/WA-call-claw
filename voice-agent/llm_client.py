"""Mimo v2.5 LLM client via OpenAI-compatible chat completions API.

No API keys are stored in this file. Keys are passed at runtime via
environment variables or constructor arguments.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

LOG = logging.getLogger("meowcaller.llm")


class LLMError(RuntimeError):
    """LLM request/response error."""


class MimoLLMClient:
    """Streaming chat completions client for Mimo v2.5.

    Uses the OpenAI-compatible ``/chat/completions`` endpoint with
    ``stream=true`` to get token-by-token responses.
    """

    def __init__(
        self,
        base_url: str = "https://token-plan-sgp.xiaomimimo.com/v1",
        api_key: str = "",
        model: str = "mimo-v2.5",
        *,
        timeout: float = 30.0,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> None:
        if not api_key:
            raise LLMError("LLM API key must be provided")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

    @property
    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def complete_streaming(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str = "",
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas from a streaming chat completion.

        If *system_prompt* is provided it is prepended as a system message.
        """
        full_messages: list[dict[str, str]] = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "stream": True,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    self._endpoint,
                    headers=self._headers,
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices or not isinstance(choices[0], dict):
                            continue
                        delta_obj = choices[0].get("delta") or {}
                        if not isinstance(delta_obj, dict):
                            continue
                        delta = delta_obj.get("content", "") or ""
                        if delta:
                            yield delta
            except httpx.HTTPStatusError as exc:
                raise LLMError(
                    f"LLM HTTP {exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            except httpx.RequestError as exc:
                raise LLMError(f"LLM request error: {exc}") from exc

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str = "",
        max_tokens: int | None = None,
    ) -> str:
        """Non-streaming convenience: collect all deltas into one string."""
        parts: list[str] = []
        async for delta in self.complete_streaming(
            messages, system_prompt=system_prompt, max_tokens=max_tokens
        ):
            parts.append(delta)
        return "".join(parts)


def build_mimo_client_from_env() -> MimoLLMClient:
    """Construct a client from ``MIMO_BASE_URL``, ``MIMO_API_KEY``, ``MIMO_MODEL``."""
    import os

    return MimoLLMClient(
        base_url=os.getenv(
            "MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"
        ),
        api_key=os.getenv("MIMO_API_KEY", ""),
        model=os.getenv("MIMO_MODEL", "mimo-v2.5"),
    )
