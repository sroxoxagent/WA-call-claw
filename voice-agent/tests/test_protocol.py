"""Tests for MEOWcaller bridge protocol parsing."""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (
    AgentHello,
    AudioDone,
    AudioPlaying,
    build_agent_hello,
    classify_bridge_event,
    parse_bridge_event,
)


def test_parse_bridge_event_valid():
    raw = json.dumps({"type": "call_started", "call_id": "abc-123", "caller_id": "+6281234"})
    event = parse_bridge_event(raw)
    assert event is not None
    assert event["type"] == "call_started"
    assert event["call_id"] == "abc-123"


def test_parse_bridge_event_invalid_json():
    assert parse_bridge_event("not json at all") is None


def test_parse_bridge_event_non_dict():
    assert parse_bridge_event('"just a string"') is None
    assert parse_bridge_event("[1, 2, 3]") is None


def test_classify_call_started():
    assert classify_bridge_event({"type": "call_started"}) == "call_started"
    assert classify_bridge_event({"type": "call_start"}) == "call_started"


def test_classify_call_ended():
    assert classify_bridge_event({"type": "call_ended"}) == "call_ended"
    assert classify_bridge_event({"type": "call_end"}) == "call_ended"


def test_classify_error():
    assert classify_bridge_event({"type": "error"}) == "error"


def test_classify_disconnect():
    assert classify_bridge_event({"type": "disconnect"}) == "disconnect"


def test_classify_cancel():
    assert classify_bridge_event({"type": "cancel"}) == "cancel"
    assert classify_bridge_event({"type": "cancelled"}) == "cancel"


def test_classify_unknown():
    assert classify_bridge_event({"type": "something_new"}) == "unknown"
    assert classify_bridge_event({}) == "unknown"


def test_agent_hello_json():
    hello = AgentHello(agent_id="test-agent-001")
    data = json.loads(hello.to_json())
    assert data["type"] == "agent_hello"
    assert data["agent_id"] == "test-agent-001"
    assert data["protocol_version"] == 1
    assert "stt" in data["capabilities"]


def test_build_agent_hello():
    raw = build_agent_hello("my-agent", {"stt": "scribe", "tts": "eleven"})
    data = json.loads(raw)
    assert data["agent_id"] == "my-agent"
    assert data["capabilities"]["tts"] == "eleven"


def test_audio_playing_json():
    msg = AudioPlaying(call_id="call-42")
    data = json.loads(msg.to_json())
    assert data["type"] == "audio_playing"
    assert data["call_id"] == "call-42"


def test_audio_done_json():
    msg = AudioDone(call_id="call-42")
    data = json.loads(msg.to_json())
    assert data["type"] == "audio_done"
    assert data["call_id"] == "call-42"


def test_classify_event_field_alias():
    """Event type may come from 'event' field instead of 'type'."""
    assert classify_bridge_event({"event": "call_started"}) == "call_started"
    assert classify_bridge_event({"event": "cancel"}) == "cancel"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
