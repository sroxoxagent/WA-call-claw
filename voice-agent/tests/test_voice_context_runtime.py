"""Runtime integration tests for caller identity and explicit MD context."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meowcaller_openclaw_voice import OpenClawVoiceAgent
from voice_context_router import (
    extract_caller_ids_from_event,
    extract_canonical_phone_from_event,
)


def make_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        bridge_url="ws://localhost:9090/ws",
        gateway_url="ws://localhost:18789",
        gateway_token="test-token",
        gateway_timeout=5.0,
        connect_timeout=5.0,
        session_timeout=5.0,
        agent_id="test-agent",
        elevenlabs_url="wss://example.invalid/stt",
        model_id="scribe_v2_realtime",
        language_code="id",
        context_config=config_path,
    )


def write_config(tmp_path: Path, *, known_jid: str = "6281111111111@s.whatsapp.net") -> Path:
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / "chat-whatsapp-direct-+6281111111111.memory.md").write_text(
        "Preferred language: Indonesian.\n", encoding="utf-8"
    )
    for name, text in {
        "IDENTITY.md": "Assistant identity: test assistant.",
        "USER.md": "Validated owner profile: test only.",
        "SOUL.md": "Be concise and safe.",
        "MEMORY.md": "Universal test rule: no historical chat.",
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")

    config = {
        "identity_mappings": {"jid": {known_jid: "+6281111111111"}, "lid": {}},
        "memory_base_dir": str(memory_dir),
        "memory_allowlist": ["chat-whatsapp-direct-*.memory.md"],
        "allow_unknown_callers": True,
        "context_profile": {
            "base_path": str(tmp_path),
            "files": [
                {"name": "IDENTITY", "path": "IDENTITY.md", "max_chars": 4000},
                {"name": "USER", "path": "USER.md", "max_chars": 4000},
                {"name": "SOUL", "path": "SOUL.md", "max_chars": 4000},
                {"name": "MEMORY", "path": "MEMORY.md", "max_chars": 4000},
            ],
            "total_max_chars": 12000,
        },
    }
    path = tmp_path / "voice-context.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_extract_caller_ids_never_guesses_bare_caller_id() -> None:
    assert extract_caller_ids_from_event({"caller_id": "999999999999999"}) == (None, None)
    assert extract_caller_ids_from_event({"caller_id": "999999999999999@lid"}) == (
        None,
        "999999999999999@lid",
    )
    assert extract_caller_ids_from_event(
        {"caller_jid": "6281111111111@s.whatsapp.net", "caller_lid": "abc@lid"}
    ) == ("6281111111111@s.whatsapp.net", "abc@lid")
    remote_event = {
        "caller_id": "999999999999998@lid",
        "remote_jid": "999999999999998@lid",
        "caller_jid": "6281111111111@s.whatsapp.net",
        "remote_phone": "+6281234567890",
    }
    assert extract_caller_ids_from_event(remote_event) == (None, "999999999999998@lid")
    assert extract_canonical_phone_from_event(remote_event) == "+6281234567890"


def test_known_jid_resolves_identity_without_loading_session_memory(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    agent = OpenClawVoiceAgent(make_args(str(config_path)), api_key="test-key")

    ctx = agent._resolve_call_context(
        "call-known", "6281111111111@s.whatsapp.net", None
    )

    assert ctx is not None
    assert ctx.identity.canonical_phone == "+6281111111111"
    assert ctx.is_restricted is False
    assert ctx.memory_text == ""
    assert ctx.memory_path is None
    assert agent._profile_loader is not None
    assert "Universal test rule" in agent._profile_loader.load_context_text()


def test_bridge_phone_override_marks_lid_caller_known(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    agent = OpenClawVoiceAgent(make_args(str(config_path)), api_key="test-key")

    ctx = agent._resolve_call_context(
        "call-phone-override",
        None,
        "999999999999998@lid",
        "+6281111111111",
    )

    assert ctx is not None
    assert ctx.identity.canonical_phone == "+6281111111111"
    assert ctx.identity.is_known is True
    assert ctx.identity.is_mapped is True
    assert ctx.is_restricted is False


def test_unknown_caller_does_not_receive_owner_md(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    agent = OpenClawVoiceAgent(make_args(str(config_path)), api_key="test-key")
    ctx = agent._resolve_call_context("call-unknown", None, "999999999999@lid")
    assert ctx is not None and ctx.is_restricted is True

    agent._current_context = ctx
    system_context = agent._build_system_context("call-unknown")
    assert "Universal test rule" not in system_context
    assert "Validated owner profile" not in system_context
    assert "Unknown caller" in system_context


@pytest.mark.asyncio
async def test_runtime_sends_resolved_session_key_without_manual_memory(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    agent = OpenClawVoiceAgent(make_args(str(config_path)), api_key="test-key")
    agent.current_call_id = "call-known"
    agent._session_generation = 1
    agent._current_context = agent._resolve_call_context(
        "call-known", "6281111111111@s.whatsapp.net", None
    )
    agent._resolved_session_key = "agent:main:whatsapp:direct:+6281111111111"

    captured: list[tuple[str, str]] = []

    async def send_chat(session_key, message, timeout=5.0):
        captured.append((session_key, message))
        yield {"type": "ack", "runId": "run-1", "status": "started"}
        yield {"type": "delta", "state": "delta", "message": "Jawaban test."}
        yield {"type": "timeout", "state": "timeout", "message": ""}

    gateway = AsyncMock()
    gateway.send_chat = send_chat
    agent.gateway = gateway
    agent.bridge = AsyncMock()

    async def no_audio(_text):
        if False:
            yield b""

    agent.tts = AsyncMock()
    agent.tts.synthesize_streaming = no_audio

    await agent._process_voice_turn("Halo")

    assert len(captured) == 1
    session_key, user_message = captured[0]
    assert session_key == "agent:main:whatsapp:direct:+6281111111111"
    assert "Halo" in user_message
    # Only voice/profile instructions are embedded. The existing session key
    # tells Gateway to load the session context automatically.
    assert "Universal test rule" in user_message
    assert "Preferred language: Indonesian." not in user_message
    assert "Session Memory" not in user_message
    assert "WhatsApp voice call" in user_message
    assert "read aloud by TTS" in user_message
    assert "Do not use emojis" in user_message
    assert "Keep the explanation concise" in user_message
