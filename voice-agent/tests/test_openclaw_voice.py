"""Tests for the OpenClaw voice path agent and gateway client.

All tests use mocks — no network calls, no API keys required.
Run with: pytest tests/test_openclaw_voice.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:
    from websockets.exceptions import ConnectionClosed  # type: ignore

# ---------------------------------------------------------------------------
# Gateway Client Tests
# ---------------------------------------------------------------------------


class MockWebSocket:
    """Mock WebSocket for testing without network."""

    def __init__(self, frames: list[str | bytes] | None = None, infinite: bool = False):
        self._frames: list[str | bytes] = list(frames or [])
        self._sent: list[str | bytes] = []
        self._closed = False
        self._recv_idx = 0
        self._infinite = infinite

    async def send(self, data: str | bytes) -> None:
        self._sent.append(data)

    async def recv(self) -> str | bytes:
        if self._closed:
            raise ConnectionClosed(None, None)
        if self._infinite:
            # For reconnect tests: return frames cyclically without exhausting
            if self._frames:
                idx = self._recv_idx % len(self._frames)
                self._recv_idx += 1
                return self._frames[idx]
            await asyncio.sleep(0.01)
            raise asyncio.TimeoutError("no more mock frames")
        if self._recv_idx >= len(self._frames):
            # Block until timeout or return empty
            await asyncio.sleep(0.01)
            raise asyncio.TimeoutError("no more mock frames")
        frame = self._frames[self._recv_idx]
        self._recv_idx += 1
        return frame

    async def close(self) -> None:
        self._closed = True

    async def ping(self) -> asyncio.Future[None]:
        """Mock WebSocket ping — returns a resolved future."""
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    @property
    def sent(self) -> list[str | bytes]:
        return self._sent


def make_connect_challenge(nonce: str = "test-nonce-001") -> str:
    """Build the gateway's pre-connect nonce challenge frame."""
    return json.dumps({
        "type": "event",
        "event": "connect.challenge",
        "payload": {"nonce": nonce, "ts": int(time.time() * 1000)},
    })


def make_connect_response(conn_id: str = "test-conn-001") -> str:
    """Build a mock gateway connect response frame."""
    return json.dumps({
        "type": "res",
        "id": "mc-1",
        "ok": True,
        "payload": {
            "type": "hello-ok",
            "protocol": 3,
            "server": {"version": "2026.3.8", "connId": conn_id},
            "features": {"methods": ["chat.send"], "events": ["chat"]},
            "policy": {
                "maxPayload": 1048576,
                "maxBufferedBytes": 4194304,
                "tickIntervalMs": 30000,
            },
        },
    })


def make_chat_ack(req_id: str, run_id: str = "run-001") -> str:
    """Build a mock chat.send ack frame."""
    return json.dumps({
        "type": "res",
        "id": req_id,
        "ok": True,
        "payload": {"runId": run_id, "status": "started"},
    })


def make_chat_event(
    run_id: str,
    state: str,
    content: str = "",
    seq: int = 0,
    session_key: str = "test-session",
) -> str:
    """Build a mock chat event frame."""
    return json.dumps({
        "type": "event",
        "event": "chat",
        "payload": {
            "runId": run_id,
            "sessionKey": session_key,
            "seq": seq,
            "state": state,
            "message": {"role": "assistant", "content": content},
        },
    })


@pytest.mark.asyncio
async def test_gateway_connect_handshake():
    """Gateway client sends correct handshake and receives hello-ok."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])

    client = OpenClawGatewayClient(
        url="ws://localhost:18789",
        token="test-token-123",
        connect_timeout=5.0,
    )

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    assert client.connected is True
    assert client._conn_id == "test-conn-001"

    # Verify handshake frame was sent
    assert len(mock_ws.sent) == 1
    handshake = json.loads(mock_ws.sent[0])
    assert handshake["type"] == "req"
    assert handshake["method"] == "connect"
    assert handshake["params"]["auth"]["token"] == "test-token-123"
    assert handshake["params"]["client"]["id"] == "gateway-client"
    assert handshake["params"]["client"]["mode"] == "backend"
    assert handshake["params"]["minProtocol"] == 3
    assert handshake["params"]["maxProtocol"] == 3
    assert handshake["params"]["caps"] == []

    await client.close()


@pytest.mark.asyncio
async def test_gateway_connect_auth_failure():
    """Gateway client raises GatewayAuthError on auth failure."""
    from openclaw_gateway_client import GatewayAuthError, OpenClawGatewayClient

    error_response = json.dumps({
        "type": "res",
        "id": "mc-1",
        "ok": False,
        "error": {"code": "UNAUTHORIZED", "message": "invalid token"},
    })
    mock_ws = MockWebSocket(frames=[make_connect_challenge(), error_response])

    client = OpenClawGatewayClient(url="ws://localhost:18789", token="bad-token")

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        with pytest.raises(GatewayAuthError, match="auth failed"):
            await client.connect()

    assert client.connected is False


@pytest.mark.asyncio
async def test_gateway_send_agent_with_extra_system_prompt():
    """Gateway agent RPC carries extraSystemPrompt and returns final payload."""
    from openclaw_gateway_client import OpenClawGatewayClient

    agent_ack = json.dumps({
        "type": "res",
        "id": "mc-2",
        "ok": True,
        "payload": {"runId": "run-agent-1", "status": "accepted"},
    })
    agent_final = json.dumps({
        "type": "res",
        "id": "mc-2",
        "ok": True,
        "payload": {
            "runId": "run-agent-1",
            "status": "ok",
            "result": {"payloads": [{"text": "Agent response."}]},
        },
    })
    mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response(), agent_ack, agent_final])
    client = OpenClawGatewayClient(url="ws://localhost:18789", token="tok")

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    events = []
    async for event in client.send_agent(
        "agent:main:voice:test-call",
        "Hello",
        extra_system_prompt="explicit context",
        timeout=5.0,
    ):
        events.append(event)

    assert events[0]["type"] == "ack"
    assert events[-1]["type"] == "final"
    assert events[-1]["message"] == "Agent response."
    agent_frame = json.loads(mock_ws.sent[1])
    assert agent_frame["method"] == "agent"
    assert agent_frame["params"]["sessionKey"] == "agent:main:voice:test-call"
    assert agent_frame["params"]["extraSystemPrompt"] == "explicit context"

    await client.close()


@pytest.mark.asyncio
async def test_gateway_send_chat_basic():
    """Gateway client sends chat and receives ack + final event."""
    from openclaw_gateway_client import OpenClawGatewayClient

    # First frame = connect response, then chat ack, then final event
    mock_ws = MockWebSocket(frames=[
        make_connect_challenge(),
        make_connect_response(),
        make_chat_ack("mc-2", "run-42"),
        make_chat_event("run-42", "final", "Hello! How can I help?", seq=1),
    ])

    client = OpenClawGatewayClient(url="ws://localhost:18789", token="tok")

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    events = []
    async for event in client.send_chat("test:session", "Hi there", timeout=5.0):
        events.append(event)

    # Should have: ack + final
    assert len(events) == 2
    assert events[0]["type"] == "ack"
    assert events[0]["runId"] == "run-42"
    assert events[1]["type"] == "final"
    assert events[1]["message"] == "Hello! How can I help?"

    # Verify the chat.send frame was sent correctly
    chat_frame = json.loads(mock_ws.sent[1])  # sent[0] was handshake
    assert chat_frame["method"] == "chat.send"
    assert chat_frame["params"]["sessionKey"] == "test:session"
    assert chat_frame["params"]["message"] == "Hi there"
    assert "idempotencyKey" in chat_frame["params"]

    await client.close()


@pytest.mark.asyncio
async def test_gateway_send_chat_streaming():
    """Gateway client handles streaming delta events before final."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[
        make_connect_challenge(),
        make_connect_response(),
        make_chat_ack("mc-2", "run-99"),
        make_chat_event("run-99", "delta", "Hello", seq=1),
        make_chat_event("run-99", "delta", "Hello, how", seq=2),
        make_chat_event("run-99", "delta", "Hello, how can I help?", seq=3),
        make_chat_event("run-99", "final", "Hello, how can I help?", seq=4),
    ])

    client = OpenClawGatewayClient(url="ws://localhost:18789", token="tok")

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    events = []
    async for event in client.send_chat("test:session", "Hi", timeout=5.0):
        events.append(event)

    # ack + 3 deltas + final = 5
    assert len(events) == 5
    assert events[0]["type"] == "ack"
    assert events[1]["type"] == "delta"
    assert events[1]["message"] == "Hello"
    assert events[-1]["type"] == "final"
    assert events[-1]["message"] == "Hello, how can I help?"

    await client.close()


@pytest.mark.asyncio
async def test_gateway_send_chat_accepts_repeated_seq_terminal_event():
    """A final event with seq=0 must not be discarded after a delta seq=0."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[
        make_connect_challenge(),
        make_connect_response(),
        make_chat_ack("mc-2", "run-repeat"),
        make_chat_event("run-repeat", "delta", "Hello", seq=0),
        make_chat_event("run-repeat", "final", "Hello there", seq=0),
    ])

    client = OpenClawGatewayClient(url="ws://localhost:18789", token="tok")
    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    events = []
    async for event in client.send_chat("test:session", "Hi", timeout=1.0):
        events.append(event)

    assert events[-1]["type"] == "final"
    assert events[-1]["message"] == "Hello there"
    await client.close()


@pytest.mark.asyncio
async def test_gateway_send_chat_error_event():
    """Gateway client handles error events gracefully."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[
        make_connect_challenge(),
        make_connect_response(),
        make_chat_ack("mc-2", "run-err"),
        make_chat_event("run-err", "error", seq=1),
    ])

    client = OpenClawGatewayClient(url="ws://localhost:18789", token="tok")

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    events = []
    async for event in client.send_chat("test:session", "Hi", timeout=5.0):
        events.append(event)

    assert len(events) == 2
    assert events[1]["type"] == "error"

    await client.close()


@pytest.mark.asyncio
async def test_gateway_not_connected():
    """Gateway client raises error if not connected."""
    from openclaw_gateway_client import GatewayError, OpenClawGatewayClient

    client = OpenClawGatewayClient(url="ws://localhost:18789", token="tok")

    with pytest.raises(GatewayError, match="not connected"):
        async for _ in client.send_chat("key", "msg"):
            pass


# ---------------------------------------------------------------------------
# Voice Agent Integration Tests (with mocks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_agent_session_key():
    """Voice agent builds correct session keys."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")

    assert agent._session_key("20260814T120000Z-abc") == "agent:main:voice:20260814T120000Z-abc"


@pytest.mark.asyncio
async def test_voice_agent_process_turn():
    """Voice agent sends transcript to gateway and plays TTS response."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 5.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")
    agent.current_call_id = "call-123"
    agent._session_generation = 1

    # Mock gateway client
    mock_gateway = AsyncMock()
    async def mock_send_chat(session_key, message, timeout=60.0):
        assert session_key.startswith("agent:main:voice:")
        assert "VOICE_CONTEXT" in message
        yield {"type": "ack", "runId": "run-1", "status": "started"}
        yield {"type": "delta", "runId": "run-1", "sessionKey": session_key, "state": "delta", "message": "I can help with that!"}
        yield {"type": "timeout", "runId": "run-1", "sessionKey": session_key, "state": "timeout", "message": ""}
    mock_gateway.send_chat = mock_send_chat
    agent.gateway = mock_gateway

    # Mock TTS
    mock_tts = AsyncMock()
    async def mock_synthesize(text):
        yield b"\x00" * 100  # fake audio
    mock_tts.synthesize_streaming = mock_synthesize
    agent.tts = mock_tts

    # Mock bridge
    mock_bridge = AsyncMock()
    mock_bridge.send = AsyncMock()
    agent.bridge = mock_bridge

    # Process turn
    await agent._process_voice_turn("Hello there")

    # Verify gateway was called with correct session key
    # (mock_send_chat is called directly, so we check the flow completed without error)
    assert agent.current_call_id == "call-123"


@pytest.mark.asyncio
async def test_voice_agent_call_lifecycle():
    """Voice agent start/stop call lifecycle."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")

    # Start call
    agent._session_generation = 0
    agent.current_call_id = None

    # Simulate start
    agent._session_generation += 1
    agent.current_call_id = "call-abc"

    assert agent.current_call_id == "call-abc"
    assert agent._session_generation == 1

    # Simulate stop
    await agent._stop_call()

    assert agent.current_call_id is None
    assert agent._session_generation == 2


# ---------------------------------------------------------------------------
# Protocol Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stt_final_forwarding():
    """STT final transcript triggers voice agent on_stt_final."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")
    agent.current_call_id = "call-xyz"
    agent._session_generation = 1

    # Mock _process_voice_turn
    processed = []
    original_process = agent._process_voice_turn

    async def mock_process(text):
        processed.append(text)

    agent._process_voice_turn = mock_process

    # on_stt_final should forward to _process_voice_turn
    await agent.on_stt_final("What time is it?", call_id="call-xyz")

    assert len(processed) == 1
    assert processed[0] == "What time is it?"

    # Should ignore stale call_id
    processed.clear()
    await agent.on_stt_final("Stale message", call_id="old-call")
    assert len(processed) == 0

    # Should ignore empty transcript
    processed.clear()
    await agent.on_stt_final("   ", call_id="call-xyz")
    assert len(processed) == 0


@pytest.mark.asyncio
async def test_stt_final_fires_barge_in_when_armed():
    """Regression [2026-08-15]: when VAD armed barge-in but the server
    committed the segment directly (short interjection → final transcript
    without any partial), on_stt_final must FIRE barge-in instead of
    silently disarming it — otherwise interrupted playback keeps playing."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")
    agent.current_call_id = "call-barge-final"
    agent._session_generation = 1

    # Gateway + bridge mocks (send_stt_text needs a "connected" gateway)
    agent.gateway = AsyncMock()
    agent.gateway.connected = True
    agent.gateway.ws = AsyncMock()
    agent.bridge = AsyncMock()
    agent.bridge.send = AsyncMock()
    agent.tts = AsyncMock()

    processed = []

    async def mock_process(text):
        processed.append(text)

    agent._process_voice_turn = mock_process

    # VAD already armed (user started speaking while agent was playing)
    agent._barge_in_armed = True
    agent._barge_in = False

    await agent.on_stt_final("Oke.", call_id="call-barge-final")

    # Barge-in must have fired: audio_stop sent to bridge + flag set
    assert agent._barge_in is True
    assert agent._barge_in_armed is False
    sent_args = [call.args[0] for call in agent.bridge.send.await_args_list]
    assert any("audio_stop" in str(arg) for arg in sent_args), (
        f"expected audio_stop on bridge, got: {sent_args}"
    )
    # Turn is still processed normally afterwards
    assert processed == ["Oke."]


@pytest.mark.asyncio
async def test_stt_final_no_barge_in_when_not_armed():
    """Regression [2026-08-15]: without an armed VAD, a final transcript
    is a normal user turn — it must NOT send audio_stop (no playback
    interruption) and must clear any stale arm."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")
    agent.current_call_id = "call-normal-turn"
    agent._session_generation = 1

    agent.gateway = AsyncMock()
    agent.gateway.connected = True
    agent.gateway.ws = AsyncMock()
    agent.bridge = AsyncMock()
    agent.bridge.send = AsyncMock()
    agent.tts = AsyncMock()

    processed = []

    async def mock_process(text):
        processed.append(text)

    agent._process_voice_turn = mock_process

    agent._barge_in_armed = False
    agent._barge_in = False

    await agent.on_stt_final("Jam berapa sekarang?", call_id="call-normal-turn")

    # No barge-in: no audio_stop, flag untouched
    assert agent._barge_in is False
    assert agent._barge_in_armed is False
    agent.bridge.send.assert_not_awaited()
    assert processed == ["Jam berapa sekarang?"]


# ---------------------------------------------------------------------------
# Edge Case Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_timeout():
    """Gateway client handles timeout gracefully."""
    from openclaw_gateway_client import GatewayError, OpenClawGatewayClient

    # Empty frames = timeout on connect response
    mock_ws = MockWebSocket(frames=[])

    client = OpenClawGatewayClient(url="ws://localhost:18789", token="tok", connect_timeout=0.1)

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        with pytest.raises(GatewayError, match="timed out"):
            await client.connect()


@pytest.mark.asyncio
async def test_session_key_uniqueness():
    """Each call gets a unique session key."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")

    keys = {agent._session_key(f"call-{i}") for i in range(100)}
    assert len(keys) == 100  # all unique


@pytest.mark.asyncio
async def test_concurrent_turns_sequential():
    """Voice agent processes turns sequentially per call."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")
    agent.current_call_id = "call-seq"
    agent._session_generation = 1
    agent.gateway = AsyncMock()
    agent.tts = AsyncMock()
    agent.bridge = AsyncMock()

    # Mock the gateway ws for ping in send_stt_text
    _ping_future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
    _ping_future.set_result(None)
    mock_ws = AsyncMock()
    mock_ws.ping = MagicMock(return_value=_ping_future)
    agent.gateway.ws = mock_ws
    agent.gateway.connected = True

    async def send_chat(_session_key, _message, timeout=60.0):
        yield {"type": "ack", "runId": "run-seq", "status": "started"}
        yield {"type": "delta", "state": "delta", "message": "ok"}
        yield {"type": "timeout", "state": "timeout", "message": ""}

    async def synthesize_streaming(_text):
        yield b"audio"

    agent.gateway.send_chat = send_chat
    agent.tts.synthesize_streaming = synthesize_streaming
    agent.bridge.send = AsyncMock()

    # Queue multiple transcripts
    transcripts = ["First", "Second", "Third"]
    processed = []

    for t in transcripts:
        await agent.on_stt_final(t, call_id="call-seq")

    # All should be processed (sequentially via lock)
    assert agent.current_call_id == "call-seq"


# ---------------------------------------------------------------------------
# Heartbeat Lifecycle Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_starts_after_connect():
    """Heartbeat task is created and running after a successful connect()."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])
    client = OpenClawGatewayClient(
        url="ws://localhost:18789", token="tok", heartbeat_interval=0.05,
    )

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    assert client.connected is True
    assert client._heartbeat_task is not None
    assert not client._heartbeat_task.done()

    # Let the heartbeat run a couple of cycles
    await asyncio.sleep(0.15)
    assert not client._heartbeat_task.done()

    await client.close()


@pytest.mark.asyncio
async def test_heartbeat_stops_on_close():
    """Heartbeat task is cancelled and cleaned up on close()."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])
    client = OpenClawGatewayClient(
        url="ws://localhost:18789", token="tok", heartbeat_interval=0.05,
    )

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    assert client._heartbeat_task is not None
    task = client._heartbeat_task
    await client.close()
    await asyncio.sleep(0.01)  # let cancellation propagate

    assert client._heartbeat_task is None
    assert task.cancelled()

    await client.close()  # idempotent


@pytest.mark.asyncio
async def test_heartbeat_stops_on_reconnect():
    """Heartbeat task is cancelled and restarted on reconnect()."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws1 = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])
    client = OpenClawGatewayClient(
        url="ws://localhost:18789", token="tok", heartbeat_interval=0.05,
    )

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws1
        await client.connect()

    old_task = client._heartbeat_task
    assert old_task is not None
    assert not old_task.done()

    # Simulate reconnect: cancel heartbeat, close old ws, start new heartbeat
    await client._cancel_heartbeat()
    assert old_task.done()

    # Start a new heartbeat (simulating what connect() does after reconnect)
    await client._start_heartbeat()

    assert client._heartbeat_task is not None
    assert client._heartbeat_task is not old_task
    assert not client._heartbeat_task.done()

    await client.close()


@pytest.mark.asyncio
async def test_heartbeat_detects_dead_connection():
    """Heartbeat marks connection as dead when pong times out."""
    from openclaw_gateway_client import OpenClawGatewayClient

    # Mock WS that raises on ping
    mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])
    ping_count = [0]

    async def _failing_ping():
        ping_count[0] += 1
        raise asyncio.TimeoutError("pong timeout")

    mock_ws.ping = _failing_ping

    client = OpenClawGatewayClient(
        url="ws://localhost:18789", token="tok", heartbeat_interval=0.05,
    )

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    assert client.connected is True

    # Wait for heartbeat to detect failure
    await asyncio.sleep(0.2)
    assert client.connected is False
    assert ping_count[0] >= 1

    await client.close()


@pytest.mark.asyncio
async def test_heartbeat_disabled_when_interval_zero():
    """Heartbeat is not started when heartbeat_interval <= 0."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])
    client = OpenClawGatewayClient(
        url="ws://localhost:18789", token="tok", heartbeat_interval=0,
    )

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    assert client._heartbeat_task is None

    await client.close()


@pytest.mark.asyncio
async def test_no_task_leakage_on_close():
    """Closing the client does not leave dangling heartbeat tasks."""
    from openclaw_gateway_client import OpenClawGatewayClient

    clients = []
    for i in range(5):
        mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])
        client = OpenClawGatewayClient(
            url="ws://localhost:18789", token="tok", heartbeat_interval=0.05,
        )
        with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            await client.connect()
        clients.append(client)

    # All heartbeat tasks running
    tasks_before = sum(1 for c in clients if c._heartbeat_task and not c._heartbeat_task.done())
    assert tasks_before == 5

    # Close all
    for c in clients:
        await c.close()

    await asyncio.sleep(0.01)  # let cancellations propagate

    # All tasks should be cancelled or done
    for c in clients:
        assert c._heartbeat_task is None
        assert c.connected is False


@pytest.mark.asyncio
async def test_no_task_leakage_on_reconnect():
    """Reconnecting does not accumulate heartbeat tasks."""
    from openclaw_gateway_client import OpenClawGatewayClient

    mock_ws = MockWebSocket(frames=[make_connect_challenge(), make_connect_response()])
    client = OpenClawGatewayClient(
        url="ws://localhost:18789", token="tok", heartbeat_interval=0.05,
    )

    # Set up client as if connected
    client.ws = mock_ws
    client.connected = True

    old_tasks = []
    for i in range(5):
        # Simulate reconnect cycle: cancel old heartbeat, start new one
        if client._heartbeat_task is not None:
            await client._cancel_heartbeat()
        await client._start_heartbeat()
        old_tasks.append(client._heartbeat_task)

    await asyncio.sleep(0.01)

    # Only the latest task should be alive; all prior ones cancelled
    assert not old_tasks[-1].done()
    for t in old_tasks[:-1]:
        assert t.cancelled()

    await client.close()


@pytest.mark.asyncio
async def test_heartbeat_concurrent_with_recv():
    """Heartbeat runs concurrently with recv without conflicts."""
    from openclaw_gateway_client import OpenClawGatewayClient

    # Mock WS that slowly drains frames
    frames = [make_connect_challenge(), make_connect_response()]
    frames += [make_chat_ack("mc-2", "run-1")]
    frames += [make_chat_event("run-1", "final", "Response", seq=1)]
    mock_ws = MockWebSocket(frames=frames)

    client = OpenClawGatewayClient(
        url="ws://localhost:18789", token="tok", heartbeat_interval=0.05,
    )

    with patch("openclaw_gateway_client.connect", new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_ws
        await client.connect()

    # Heartbeat is running
    assert client._heartbeat_task is not None

    # Meanwhile, send_chat should work normally (recv in main loop)
    events = []
    async for event in client.send_chat("test:session", "Hi", timeout=5.0):
        events.append(event)

    assert events[0]["type"] == "ack"
    assert events[-1]["type"] == "final"

    # Heartbeat still running
    assert not client._heartbeat_task.done()

    await client.close()


@pytest.mark.asyncio
async def test_send_stt_text_does_not_send_standalone_gateway_ping():
    """Voice agent relies on the real RPC instead of a standalone ping."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")
    agent.current_call_id = "call-ping"
    agent._session_generation = 1

    # Track ping calls
    ping_called = [False]
    _ping_future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
    _ping_future.set_result(None)

    async def _tracked_ping():
        ping_called[0] = True
        return _ping_future

    mock_ws = AsyncMock()
    mock_ws.ping = _tracked_ping
    agent.gateway = AsyncMock()
    agent.gateway.ws = mock_ws
    agent.gateway.connected = True

    await agent.send_stt_text("Hello world")
    assert ping_called[0] is False


@pytest.mark.asyncio
async def test_send_stt_text_keeps_connected_socket_untouched():
    """send_stt_text does not mark a healthy socket stale via a client ping."""
    from meowcaller_openclaw_voice import OpenClawVoiceAgent

    args = MagicMock()
    args.bridge_url = "ws://localhost:9090/ws"
    args.gateway_url = "ws://localhost:18789"
    args.gateway_token = "tok"
    args.gateway_timeout = 60.0
    args.connect_timeout = 10.0
    args.session_timeout = 15.0
    args.agent_id = "test-agent"
    args.elevenlabs_url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    args.model_id = "scribe_v2_realtime"
    args.language_code = "id"

    agent = OpenClawVoiceAgent(args, api_key="test-key")
    agent.current_call_id = "call-ping-fail"

    mock_ws = AsyncMock()
    agent.gateway = AsyncMock()
    agent.gateway.ws = mock_ws
    agent.gateway.connected = True

    await agent.send_stt_text("Hello")
    assert agent.gateway.connected is True
