"""Tests for the ConversationHandler with mocked LLM and TTS.

No real API calls are made — LLM and TTS are fully mocked.
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_client import MimoLLMClient
from tts_client import ElevenLabsTTSClient
from meowcaller_converse_agent import ConversationHandler, SYSTEM_PROMPT


class MockLLM:
    """Mock MimoLLMClient that returns canned responses."""

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or ["Hello! How can I help?"])
        self.call_count = 0
        self.last_messages = None

    async def complete(self, messages, *, system_prompt="", max_tokens=None):
        self.last_messages = messages
        resp = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return resp


class MockTTS:
    """Mock ElevenLabsTTSClient that returns fake PCM bytes."""

    def __init__(self, audio_chunks: list[bytes] | None = None):
        self.audio_chunks = list(audio_chunks or [b"\x00\x00" * 100])
        self.call_count = 0
        self.last_text = None

    async def synthesize_streaming(self, text: str) -> AsyncIterator[bytes]:
        self.last_text = text
        for chunk in self.audio_chunks:
            yield chunk
        self.call_count += 1

    async def synthesize(self, text: str) -> bytes:
        self.last_text = text
        self.call_count += 1
        return b"".join(self.audio_chunks)


class MockBridge:
    """Mock WebSocket bridge that records sent messages."""

    def __init__(self):
        self.sent: list[str | bytes] = []
        self._closed = False

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self._closed = True


def _run(coro):
    """Run an async coroutine synchronously for tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_handler(llm=None, tts=None, bridge=None):
    """Create a ConversationHandler with mocks."""
    return ConversationHandler(
        llm=llm or MockLLM(),
        tts=tts or MockTTS(),
        bridge=bridge or MockBridge(),
        system_prompt=SYSTEM_PROMPT,
        chunk_bytes=320,  # small chunks for testing
    )


def test_handler_initial_state():
    handler = _make_handler()
    assert handler.conversation_history == []
    assert handler.current_call_id is None


def test_handler_reset():
    handler = _make_handler()
    handler.conversation_history = [{"role": "user", "content": "hi"}]
    handler.current_call_id = "call-1"
    handler.reset()
    assert handler.conversation_history == []
    assert handler.current_call_id is None


def test_handler_on_final_transcript_empty():
    """Empty transcript should be a no-op."""
    llm = MockLLM()
    tts = MockTTS()
    bridge = MockBridge()
    handler = _make_handler(llm=llm, tts=tts, bridge=bridge)

    _run(handler.on_final_transcript(""))
    _run(handler.on_final_transcript("   "))

    assert llm.call_count == 0
    assert tts.call_count == 0
    assert len(bridge.sent) == 0


def test_handler_on_final_transcript_triggers_llm_and_tts():
    llm = MockLLM(responses=["I am fine, thank you!"])
    tts = MockTTS(audio_chunks=[b"\x00\x00" * 50])
    bridge = MockBridge()
    handler = _make_handler(llm=llm, tts=tts, bridge=bridge)
    handler.current_call_id = "call-test"

    _run(handler.on_final_transcript("How are you?"))

    # LLM should have been called with user message
    assert len(handler.conversation_history) == 2
    assert handler.conversation_history[0] == {"role": "user", "content": "How are you?"}
    assert handler.conversation_history[1] == {"role": "assistant", "content": "I am fine, thank you!"}

    # TTS should have been called
    assert tts.last_text == "I am fine, thank you!"

    # Bridge should have received audio_playing, PCM frames, audio_done
    sent_types = []
    for msg in bridge.sent:
        if isinstance(msg, str):
            data = json.loads(msg)
            sent_types.append(data.get("type"))
        else:
            sent_types.append("pcm_frame")
    assert "audio_playing" in sent_types
    assert "pcm_frame" in sent_types
    assert "audio_done" in sent_types


def test_handler_conversation_history_grows():
    llm = MockLLM(responses=["reply 1", "reply 2"])
    tts = MockTTS()
    bridge = MockBridge()
    handler = _make_handler(llm=llm, tts=tts, bridge=bridge)
    handler.current_call_id = "call-1"

    _run(handler.on_final_transcript("first question"))
    assert len(handler.conversation_history) == 2  # user + assistant

    _run(handler.on_final_transcript("second question"))
    assert len(handler.conversation_history) == 4  # +user +assistant


def test_handler_cancel():
    """Cancel should stop TTS streaming."""
    llm = MockLLM()
    # TTS that yields multiple chunks so cancel can take effect
    async def slow_tts(text):
        for i in range(100):
            yield b"\x00\x00" * 10
    tts = MockTTS()
    tts.synthesize_streaming = slow_tts
    bridge = MockBridge()
    handler = _make_handler(llm=llm, tts=tts, bridge=bridge)
    handler.current_call_id = "call-1"

    # Start transcription and immediately cancel
    async def _run_cancel():
        task = asyncio.create_task(handler.on_final_transcript("hello"))
        await asyncio.sleep(0.01)
        handler.cancel()
        await task

    _run(_run_cancel())

    # Should have sent audio_playing
    assert any(
        (isinstance(m, str) and "audio_playing" in m)
        for m in bridge.sent
    )


def test_handler_llm_error_sends_apology():
    class FailingLLM:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("LLM is down")

    llm = FailingLLM()
    tts = MockTTS()
    bridge = MockBridge()
    handler = _make_handler(llm=llm, tts=tts, bridge=bridge)
    handler.current_call_id = "call-err"

    _run(handler.on_final_transcript("test error handling"))

    # TTS should have been called with apology text
    assert tts.last_text is not None
    assert "error" in tts.last_text.lower()


def test_handler_system_prompt():
    """Verify system prompt is set."""
    handler = _make_handler()
    assert "voice assistant" in handler.system_prompt.lower()


def test_handler_drops_outbound_when_call_session_invalid():
    llm = MockLLM(responses=["should not be sent"])
    tts = MockTTS()
    bridge = MockBridge()
    handler = _make_handler(llm=llm, tts=tts, bridge=bridge)
    handler.current_call_id = "call-old"
    handler._session_valid = lambda: False

    _run(handler.on_final_transcript("old call"))

    assert llm.call_count == 1
    assert tts.call_count == 0
    assert bridge.sent == []


def test_handler_audio_done_keeps_original_call_id():
    llm = MockLLM(responses=["reply"])
    tts = MockTTS(audio_chunks=[b"\x00\x00" * 50])
    bridge = MockBridge()
    handler = _make_handler(llm=llm, tts=tts, bridge=bridge)
    handler.current_call_id = "call-original"
    handler._session_valid = lambda: True

    _run(handler.on_final_transcript("hello"))

    control = [json.loads(m) for m in bridge.sent if isinstance(m, str)]
    assert control[0]["call_id"] == "call-original"
    assert control[-1]["call_id"] == "call-original"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
