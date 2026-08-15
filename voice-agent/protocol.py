"""MEOWcaller bridge WebSocket protocol helpers.

Parses and constructs the JSON/text control messages exchanged over the
MEOWcaller WebSocket audio bridge.  Binary frames are raw PCM audio and
are not processed here.
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from typing import Any

LOG = logging.getLogger("meowcaller.protocol")

AUDIO_MAGIC = b"MCID"
AUDIO_VERSION = 1
AUDIO_HEADER_SIZE = 7


def pack_audio_frame(call_id: str, pcm: bytes) -> bytes:
    """Wrap PCM in a binary envelope carrying its owning call ID."""
    call_id_bytes = call_id.encode("utf-8")
    if len(call_id_bytes) > 0xFFFF:
        raise ValueError("call_id is too long")
    return (
        AUDIO_MAGIC
        + bytes([AUDIO_VERSION])
        + struct.pack(">H", len(call_id_bytes))
        + call_id_bytes
        + pcm
    )


def unpack_audio_frame(data: bytes) -> tuple[str, bytes]:
    """Validate and remove the call-ID envelope from a binary frame."""
    if len(data) < AUDIO_HEADER_SIZE or data[:4] != AUDIO_MAGIC:
        raise ValueError("invalid audio frame header")
    if data[4] != AUDIO_VERSION:
        raise ValueError(f"unsupported audio frame version: {data[4]}")
    call_id_len = struct.unpack(">H", data[5:7])[0]
    end = AUDIO_HEADER_SIZE + call_id_len
    if len(data) < end:
        raise ValueError("invalid call_id length")
    return data[AUDIO_HEADER_SIZE:end].decode("utf-8"), data[end:]


@dataclass(frozen=True)
class AgentHello:
    """Outbound: announce agent capabilities on connect."""

    agent_id: str
    capabilities: dict[str, str] = field(default_factory=lambda: {"stt": "elevenlabs_scribe_v2_realtime"})

    def to_json(self) -> str:
        return json.dumps(
            {
                "type": "agent_hello",
                "agent_id": self.agent_id,
                "protocol_version": 1,
                "capabilities": self.capabilities,
            },
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class CallStarted:
    """Inbound: a new call has been initiated."""

    call_id: str
    caller_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CallEnded:
    """Inbound: the call has ended."""

    call_id: str
    reason: str = ""


@dataclass(frozen=True)
class BridgeError:
    """Inbound: an error event from the bridge."""

    message: str = ""
    code: str = ""


@dataclass(frozen=True)
class AudioPlaying:
    """Outbound: notify the bridge that bot audio playback is starting."""

    call_id: str

    def to_json(self) -> str:
        return json.dumps(
            {"type": "audio_playing", "call_id": self.call_id},
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class AudioDone:
    """Outbound: notify the bridge that bot audio playback finished."""

    call_id: str

    def to_json(self) -> str:
        return json.dumps(
            {"type": "audio_done", "call_id": self.call_id},
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class AudioStop:
    """Outbound: request the bridge to stop bot audio playback immediately.

    Used for barge-in: the caller started speaking, so any bot audio still
    queued in the bridge must be discarded instead of played out.
    """

    call_id: str

    def to_json(self) -> str:
        return json.dumps(
            {"type": "audio_stop", "call_id": self.call_id},
            separators=(",", ":"),
        )


def parse_bridge_event(raw: str) -> dict[str, Any] | None:
    """Parse a JSON text message from the bridge into a dict.

    Returns ``None`` if the message is not valid JSON or has no ``type``.
    """
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        LOG.warning("ignoring non-JSON bridge message: %s", raw[:200])
        return None
    if not isinstance(event, dict):
        LOG.warning("ignoring non-dict bridge message: %r", raw[:200])
        return None
    return event


def classify_bridge_event(event: dict[str, Any]) -> str:
    """Return a normalised event type string from a parsed bridge event.

    Recognised types: ``call_started``, ``call_ended``, ``error``,
    ``disconnect``, ``cancel``, ``unknown``.
    """
    raw_type = event.get("type", event.get("event", ""))
    mapping = {
        "call_started": "call_started",
        "call_start": "call_started",
        "call_ended": "call_ended",
        "call_end": "call_ended",
        "error": "error",
        "disconnect": "disconnect",
        "cancel": "cancel",
        "cancelled": "cancel",
    }
    return mapping.get(raw_type, "unknown")


def build_agent_hello(agent_id: str, capabilities: dict[str, str] | None = None) -> str:
    """Shortcut to build an agent_hello JSON string."""
    caps = capabilities or {"stt": "elevenlabs_scribe_v2_realtime"}
    return AgentHello(agent_id=agent_id, capabilities=caps).to_json()
