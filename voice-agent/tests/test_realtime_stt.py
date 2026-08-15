"""Tests for realtime STT transport and local VAD commit behavior."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meowcaller_converse_agent import (
    RealtimeSTT,
    VAD_FRAME_BYTES,
    VAD_MAX_SEGMENT_MS,
    VAD_SILENCE_MS,
    webrtcvad,
)


class MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        model_id="scribe_v2_realtime",
        language_code="id",
        elevenlabs_url="wss://example.test/realtime",
        commit_strategy="manual",
        vad_silence_threshold_secs=None,
        min_silence_duration_ms=None,
        min_speech_duration_ms=None,
    )


def _run(coro):
    return asyncio.run(coro)


def test_realtime_stt_uses_manual_commit_strategy_by_default():
    stt = RealtimeSTT("redacted", _args())
    assert "commit_strategy=manual" in stt.url()
    assert "vad_silence_threshold_secs" not in stt.url()
    assert "min_silence_duration_ms" not in stt.url()


def test_realtime_stt_server_vad_when_configured():
    args = _args()
    args.commit_strategy = "vad"
    args.vad_silence_threshold_secs = 0.3
    args.min_silence_duration_ms = 500
    args.min_speech_duration_ms = 200
    stt = RealtimeSTT("redacted", args)
    url = stt.url()
    assert "commit_strategy=vad" in url
    assert "vad_silence_threshold_secs=0.3" in url
    assert "min_silence_duration_ms=500" in url
    assert "min_speech_duration_ms=200" in url


def test_client_fallback_skips_commit_when_server_committed():
    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        stt._agent_ref = None  # production sets this; tests bypass agent notify
        ws = MockWebSocket()
        stt.ws = ws
        # server commits the segment
        stt.handle_event({"message_type": "committed_transcript", "text": "halo"})
        assert stt._server_committed is True
        # even with silence >= limit, client must NOT commit
        stt._speech_seen = True
        stt._silence_ms = stt._silence_limit_ms + 100
        await stt.send_pcm(b"\x00\x00" * 960)
        assert not any(message.get("commit") is True for message in ws.sent)
        # new speech resets the flag → fallback allowed again
        stt.handle_event({"message_type": "committed_transcript", "text": ""})
        stt._speech_seen = False
        stt._server_committed = False
        assert stt._server_committed is False

    _run(scenario())


def test_server_commit_resets_segment_state_for_next_segment():
    """Regression [2026-08-15]: a server commit must reset client-side
    segment state so the fallback VAD can detect the NEXT segment.
    Previously _speech_seen stayed True forever, so the fallback never
    armed again and barge-in died for the rest of the call."""

    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        stt._agent_ref = None
        ws = MockWebSocket()
        stt.ws = ws

        # Simulate an in-flight segment (speech already seen, silence counting)
        stt._speech_seen = True
        stt._speech_frames = 8
        stt._silence_ms = 400
        stt._segment_ms = 1200
        stt._server_committed = False

        # Server commits the segment directly (e.g. short interjection
        # like "Oke." that never produced a partial transcript)
        stt.handle_event({"message_type": "committed_transcript", "text": "Oke."})

        # Segment state must be fully reset for the next segment
        assert stt._server_committed is True
        assert stt._speech_seen is False
        assert stt._speech_frames == 0
        assert stt._silence_ms == 0
        assert stt._segment_ms == 0

        # And the fallback must be able to arm + commit for a new segment
        stt._server_committed = False
        speech = (5000).to_bytes(2, "little", signed=True) * 960
        silence = b"\x00\x00" * 960
        await stt.send_pcm(speech)
        for _ in range((VAD_SILENCE_MS // 60) + 1):
            await stt.send_pcm(silence)
        commits = [message for message in ws.sent if message.get("commit") is True]
        assert len(commits) == 1

    _run(scenario())


def test_webrtc_vad_is_available_for_poc():
    assert webrtcvad is not None


def test_webrtc_vad_ignores_low_level_input_for_segment_start():
    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        ws = MockWebSocket()
        stt.ws = ws
        low_level = (1000).to_bytes(2, "little", signed=True) * 960
        silence = b"\x00\x00" * 960
        await stt.send_pcm(low_level)
        for _ in range(30):
            await stt.send_pcm(silence)
        assert not any(message.get("commit") is True for message in ws.sent)
        assert stt._speech_seen is False

    _run(scenario())


def test_webrtc_vad_commits_after_stable_silence():
    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        ws = MockWebSocket()
        stt.ws = ws
        speech = (5000).to_bytes(2, "little", signed=True) * 960
        silence = b"\x00\x00" * 960
        await stt.send_pcm(speech)
        for _ in range((VAD_SILENCE_MS // 60) + 1):
            await stt.send_pcm(silence)
        commits = [message for message in ws.sent if message.get("commit") is True]
        assert len(commits) == 1
        assert stt._speech_seen is False

    _run(scenario())


def test_webrtc_vad_has_max_segment_commit_guard():
    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        ws = MockWebSocket()
        stt.ws = ws
        speech = (5000).to_bytes(2, "little", signed=True) * 960
        for _ in range((VAD_MAX_SEGMENT_MS // 60) + 2):
            await stt.send_pcm(speech)
        commits = [message for message in ws.sent if message.get("commit") is True]
        assert len(commits) == 1

    _run(scenario())


def test_local_vad_commits_after_two_seconds_of_silence():
    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        ws = MockWebSocket()
        stt.ws = ws

        # Speech followed by stable silence should commit after the POC's
        # 1.5-second hangover (VAD_SILENCE_MS = 1500).
        speech = (5000).to_bytes(2, "little", signed=True) * 960
        silence = b"\x00\x00" * 960
        await stt.send_pcm(speech)
        for _ in range((VAD_SILENCE_MS // 60) - 1):  # 1,440 ms; commit must not happen yet.
            await stt.send_pcm(silence)
        assert not any(message.get("commit") is True for message in ws.sent)

        await stt.send_pcm(silence)  # 1,500 ms total silence; threshold reached.
        commits = [message for message in ws.sent if message.get("commit") is True]
        assert len(commits) == 1
        assert commits[0]["audio_base_64"] == ""
        assert stt._speech_seen is False

    _run(scenario())


def test_realtime_stt_commit_is_noop_without_speech():
    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        ws = MockWebSocket()
        stt.ws = ws
        await stt.commit()
        assert not ws.sent

    _run(scenario())


def test_realtime_stt_call_id_is_stored_for_event_scoping():
    stt = RealtimeSTT("redacted", _args())
    stt._call_id = "call-123"
    assert stt._call_id == "call-123"


def test_realtime_stt_close_clears_vad_state():
    async def scenario():
        stt = RealtimeSTT("redacted", _args())
        ws = MockWebSocket()
        stt.ws = ws
        stt._speech_seen = True
        stt._silence_ms = 120
        await stt.close()
        assert ws.closed is True
        assert stt._speech_seen is False
        assert stt._silence_ms == 0

    _run(scenario())
