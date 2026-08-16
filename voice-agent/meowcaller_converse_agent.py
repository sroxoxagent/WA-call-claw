#!/usr/bin/env python3
"""MEOWcaller full conversational agent.

Pipeline:
  PCM in → ElevenLabs STT → Mimo v2.5 LLM → ElevenLabs TTS → PCM out

No API keys are stored in this file.  Keys are loaded from environment
variables or the existing workspace STT wrapper via the launcher script.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import math
import os
import signal
import struct
import sys
from typing import Any, Callable
from urllib.parse import urlencode

try:
    from websockets.asyncio.client import ClientConnection, connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    from websockets import connect  # type: ignore
    from websockets.legacy.client import WebSocketClientProtocol as ClientConnection  # type: ignore
    from websockets.exceptions import ConnectionClosed  # type: ignore

from audio_convert import chunk_pcm, wav_to_pcm_s16le_16k
# Telegram debug hook removed 2026-08-15 per Shendy — no outbound
# Telegram logging. Stub kept so legacy call sites are no-ops.
def debug_notify(*args, **kwargs):
    """No-op: legacy Telegram debug hook removed."""
    return None
from llm_client import LLMError, MimoLLMClient
from protocol import (
    AudioDone,
    AudioPlaying,
    build_agent_hello,
    classify_bridge_event,
    pack_audio_frame,
    parse_bridge_event,
    unpack_audio_frame,
)
from tts_client import ElevenLabsTTSClient, TTSError

try:
    import webrtcvad
except ImportError:  # pragma: no cover - exercised only on minimal environments
    webrtcvad = None  # type: ignore[assignment]

LOG = logging.getLogger("meowcaller-converse")

SYSTEM_PROMPT = (
    "You are a helpful voice assistant.  You speak naturally in the "
    "user's language (typically Indonesian or English).  Keep responses "
    "concise and conversational — suitable for a phone call.  "
    "Do NOT use markdown, bullet points, or code formatting.  "
    "Limit responses to 2-3 sentences unless the user asks for detail."
)

# Default chunk size: 100 ms of 16 kHz s16le mono = 3200 bytes
# Match MEOWcaller's 60 ms pull cadence to avoid partial outbound frames.
DEFAULT_CHUNK_BYTES = 1920
VAD_FRAME_MS = 20
VAD_SAMPLE_RATE = 16000
VAD_FRAME_BYTES = VAD_SAMPLE_RATE * VAD_FRAME_MS // 1000 * 2
VAD_MODE = 3
VAD_MIN_RMS = 1500.0
VAD_MIN_SPEECH_FRAMES = 3
VAD_SILENCE_MS = 1500
VAD_MAX_SEGMENT_MS = 30000  # 30s safety cap (was 8s) — 8s forced-commits mid-sentence
                             # when callers talk continuously, fragmenting transcripts
                             # and making the agent reply repeatedly (loop).


def pcm_rms_s16le(pcm: bytes) -> float:
    """Return RMS amplitude for mono s16le PCM, or zero for empty/odd input."""
    sample_count = len(pcm) // 2
    if sample_count <= 0:
        return 0.0
    samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])
    return math.sqrt(sum(sample * sample for sample in samples) / sample_count)


class AgentError(RuntimeError):
    """Expected agent configuration/protocol error."""


class RealtimeSTT:
    """ElevenLabs Scribe Realtime STT wrapper (unchanged interface)."""

    def __init__(self, api_key: str, args: argparse.Namespace) -> None:
        self.api_key = api_key
        self.args = args
        self.ws: ClientConnection | Any | None = None
        self.ready = asyncio.Event()
        self.closed = False
        self._speech_seen = False
        self._speech_frames = 0
        self._silence_ms = 0
        # Silence threshold for turn end; overridable per-instance from the
        # agent config file (vad.silence_ms).
        self._silence_limit_ms = VAD_SILENCE_MS
        self._segment_ms = 0
        self._vad_buffer = bytearray()
        self._vad = webrtcvad.Vad(VAD_MODE) if webrtcvad is not None else None
        self._final_emitted_text: str | None = None
        self._agent_forwarded_text: str | None = None
        self._call_id: str | None = None
        self._debug_stream_sent_words = 0
        self._commit_lock = asyncio.Lock()
        self._total_audio_frames = 0
        self._first_frame_sent = False
        # VAD provider selection (config-driven):
        #   "manual"    → client-side webrtcvad controls the turn (default)
        #   "vad"       → ElevenLabs server-side VAD commits; client webrtcvad
        #                 remains as a safety net (only commits if the server
        #                 has NOT committed this segment yet)
        self.commit_strategy = getattr(args, "commit_strategy", "manual")
        self.vad_silence_threshold_secs = getattr(args, "vad_silence_threshold_secs", None)
        self.min_silence_duration_ms = getattr(args, "min_silence_duration_ms", None)
        self.min_speech_duration_ms = getattr(args, "min_speech_duration_ms", None)
        self._server_committed = False

    def url(self) -> str:
        params = {
            "model_id": self.args.model_id,
            "audio_format": "pcm_16000",
            "language_code": self.args.language_code,
            "include_timestamps": "false",
            "include_language_detection": "true",
        }
        # VAD provider (config-driven):
        #   "vad"    → ElevenLabs server-side VAD commits turns automatically.
        #              Tune with vad_silence_threshold_secs /
        #              min_silence_duration_ms / min_speech_duration_ms.
        #   "manual" → client-side webrtcvad controls the turn (original
        #              behavior; server-side VAD once failed to emit
        #              committed transcripts in live calls).
        params["commit_strategy"] = self.commit_strategy
        if self.commit_strategy == "vad":
            if self.vad_silence_threshold_secs is not None:
                params["vad_silence_threshold_secs"] = str(self.vad_silence_threshold_secs)
            if self.min_silence_duration_ms is not None:
                params["min_silence_duration_ms"] = str(self.min_silence_duration_ms)
            if self.min_speech_duration_ms is not None:
                params["min_speech_duration_ms"] = str(self.min_speech_duration_ms)
        return f"{self.args.elevenlabs_url}?{urlencode(params)}"

    async def connect(self) -> None:
        LOG.info("connecting to ElevenLabs Realtime STT")
        self.ws = await connect(
            self.url(),
            additional_headers={"xi-api-key": self.api_key},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=self.args.connect_timeout,
        )
        LOG.info("ElevenLabs STT WebSocket connected")
        debug_notify(self._call_id, "stt_connected", f"model={self.args.model_id}")

    async def receive_events(self) -> None:
        if self.ws is None:
            raise AgentError("STT WebSocket is not connected")
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    LOG.debug("ignoring unexpected binary STT message: %d bytes", len(raw))
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    LOG.warning("ignoring non-JSON STT message")
                    continue
                self.handle_event(event)
        except ConnectionClosed as exc:
            debug_notify(self._call_id, "stt_disconnected", str(exc)[:120])
            if not self.closed:
                raise AgentError(f"ElevenLabs STT connection closed: {exc}") from exc

    def _debug_partial_stream(self, text: str) -> None:
        """Send a compact Telegram event for each newly completed five words."""
        words = text.split()
        while len(words) >= self._debug_stream_sent_words + 5:
            debug_notify(
                self._call_id,
                "stt_partial",
                f"words={len(words)} chars={len(text)}",
            )
            self._debug_stream_sent_words += 5

    def handle_event(self, event: dict[str, Any]) -> None:
        message_type = event.get("message_type", event.get("type", "unknown"))
        if message_type == "session_started":
            session_id = event.get("session_id", "unknown")
            LOG.info("STT session started: %s", session_id)
            debug_notify(self._call_id, "stt_session_ready", f"session={session_id}")
            self.ready.set()
        elif message_type == "vad_speech_start":
            # Server-side VAD (ElevenLabs) detected the start of speech.
            # More reliable than the client webrtcvad during TTS playback
            # (bridge-processed audio can defeat the aggressive client VAD).
            debug_notify(self._call_id, "VAD_SPEECH (ElevenLabs)", "")
            agent_ref = getattr(self, "_agent_ref", None)
            if agent_ref is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        agent_ref.on_user_speech_start(
                            getattr(self, "_call_id", None)
                        ),
                        name="barge-in-server-vad",
                    )
                except RuntimeError:
                    pass
        elif message_type in {"partial_transcript", "partial"}:
            text = event.get("text", "")
            if text:
                self._debug_partial_stream(text)
                print(json.dumps({"type": "transcript_partial", "text": text}, ensure_ascii=False), flush=True)
                # Streaming word confirmation: forward to the agent so
                # barge-in only fires when real words are detected, not
                # on raw VAD/noise.
                agent_ref = getattr(self, "_agent_ref", None)
                if agent_ref is not None:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(
                            agent_ref.on_partial_transcript(
                                text, getattr(self, "_call_id", None)
                            ),
                            name="stt-partial-word",
                        )
                    except RuntimeError:
                        pass
        elif message_type in {
            "committed_transcript",
            "committed_transcript_with_timestamps",
            "final_transcript",
            "final_transcript_with_timestamps",
        }:
            # Server committed this segment — the client-side fallback VAD
            # must not commit again for the same segment.
            self._server_committed = True
            # Reset segment state so the client fallback VAD can detect the
            # NEXT segment (otherwise _speech_seen stays True and the
            # fallback never arms again for the rest of the call).
            self._speech_seen = False
            self._speech_frames = 0
            self._silence_ms = 0
            self._segment_ms = 0
            text = event.get("text", "")
            if text and text != self._final_emitted_text:
                self._final_emitted_text = text
                debug_notify(self._call_id, "stt_final_received", f"chars={len(text)}")
                # Emit as JSON for downstream consumers
                print(json.dumps({"type": "transcript_final", "text": text}, ensure_ascii=False), flush=True)
        elif message_type in {"error", "auth_error", "quota_exceeded", "rate_limited", "commit_throttled", "input_error", "invalid_request"}:
            LOG.error("ElevenLabs STT error (%s): %s", message_type, event.get("error", event))
            debug_notify(getattr(self, "_call_id", None), "stt_error", f"{message_type}: {event.get('error', '')}"[:120])
        else:
            LOG.debug("STT event: %s", event)

    def _vad_is_speech(self, frame: bytes) -> bool:
        """Classify one exact 20 ms PCM frame with WebRTC VAD."""
        if self._vad is None:
            return False
        try:
            # Energy gate rejects low-level codec crackle before WebRTC VAD.
            # WebRTC remains the speech classifier; RMS is not used to open a
            # segment by itself.
            if pcm_rms_s16le(frame) < VAD_MIN_RMS:
                return False
            return bool(self._vad.is_speech(frame, VAD_SAMPLE_RATE))
        except (ValueError, TypeError):
            LOG.warning("invalid VAD frame length=%d", len(frame))
            return False

    async def send_pcm(self, pcm: bytes) -> None:
        if self.ws is None or self.closed or not pcm:
            return

        # Forward the original incoming PCM to ElevenLabs first. VAD is only
        # used to decide when to commit; it must never alter the STT audio.
        self._total_audio_frames += 1
        if not self._first_frame_sent:
            self._first_frame_sent = True
            debug_notify(
                self._call_id,
                "stt_audio_first_frame",
                f"frames=1 chunk={len(pcm)}",
            )
        message = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "commit": False,
            "sample_rate": 16000,
        }
        async with self._commit_lock:
            await self.ws.send(json.dumps(message, separators=(",", ":")))
            self._vad_buffer.extend(pcm)
            while len(self._vad_buffer) >= VAD_FRAME_BYTES:
                frame = bytes(self._vad_buffer[:VAD_FRAME_BYTES])
                del self._vad_buffer[:VAD_FRAME_BYTES]
                speech = self._vad_is_speech(frame)
                self._segment_ms += VAD_FRAME_MS
                if speech:
                    self._speech_frames += 1
                    self._silence_ms = 0
                    if self._speech_frames >= VAD_MIN_SPEECH_FRAMES and not self._speech_seen:
                        self._speech_seen = True
                        # New segment — allow the client fallback to commit
                        # again if the server VAD fails to.
                        self._server_committed = False
                        # Label: WEBRTC = client webrtcvad detected speech.
                        # (With commit_strategy=vad the ElevenLabs server is
                        # primary, but the detector is still webrtcvad, so the
                        # label stays WEBRTC — consistent with manual mode.)
                        vad_label = "VAD_SPEECH (WEBRTC)"
                        debug_notify(
                            self._call_id,
                            vad_label,
                            f"frames={self._speech_frames}",
                        )
                        # Barge-in: notify the agent that the user started
                        # speaking so it can stop playback immediately.
                        agent_ref = getattr(self, "_agent_ref", None)
                        if agent_ref is not None:
                            try:
                                loop = asyncio.get_running_loop()
                                loop.create_task(
                                    agent_ref.on_user_speech_start(
                                        getattr(self, "_call_id", None)
                                    ),
                                    name="barge-in-notify",
                                )
                            except RuntimeError:
                                pass
                else:
                    if not self._speech_seen:
                        # Require consecutive speech frames; isolated noise
                        # spikes must not open a segment.
                        self._speech_frames = 0
                    else:
                        self._silence_ms += VAD_FRAME_MS

            if self._speech_seen and not self._server_committed and (
                self._silence_ms >= self._silence_limit_ms
                or self._segment_ms >= VAD_MAX_SEGMENT_MS
            ):
                await self.commit()

    async def commit(self) -> None:
        """Explicitly commit the current STT segment."""
        if self.ws is None or self.closed or not self._speech_seen:
            return
        message = {
            "message_type": "input_audio_chunk",
            "audio_base_64": "",
            "commit": True,
            "sample_rate": 16000,
        }
        await self.ws.send(json.dumps(message, separators=(",", ":")))
        LOG.info("STT segment committed after %dms silence", self._silence_ms)
        vad_stop_label = "VAD_SPEECH_STOP (WEBRTC)"
        debug_notify(
            self._call_id,
            vad_stop_label,
            f"silence_ms={self._silence_ms}",
        )
        debug_notify(
            self._call_id,
            "stt_commit_sent",
            f"silence_ms={self._silence_ms} frames={self._total_audio_frames}",
        )
        self._speech_seen = False
        self._speech_frames = 0
        self._silence_ms = 0
        self._segment_ms = 0
        self._vad_buffer.clear()
        self._final_emitted_text = None
        self._agent_forwarded_text = None
        self._debug_stream_sent_words = 0

    async def close(self) -> None:
        if self._total_audio_frames > 0 or self._first_frame_sent:
            debug_notify(
                self._call_id,
                "stt_audio_summary",
                f"total_frames={self._total_audio_frames} first_frame={self._first_frame_sent}",
            )
        self.closed = True
        self.ready.clear()
        if self.ws is not None:
            ws = self.ws
            self.ws = None
            try:
                # Never let the close handshake block the event loop for long —
                # a slow/absent close reply from the STT server would otherwise
                # stall pong replies and get the agent disconnected by the
                # bridge ping watchdog (default websockets close_timeout=10s).
                await asyncio.wait_for(ws.close(), timeout=3)
            except asyncio.TimeoutError:
                debug_notify(self._call_id, "stt_close_timeout", "ws.close() exceeded 3s — aborting handshake")
                try:
                    ws.transport.abort()  # type: ignore[attr-defined]
                except Exception:
                    pass
            except Exception:
                pass
        self._speech_seen = False
        self._speech_frames = 0
        self._silence_ms = 0
        self._segment_ms = 0
        self._vad_buffer.clear()
        self._final_emitted_text = None
        self._agent_forwarded_text = None
        self._debug_stream_sent_words = 0


class ConversationHandler:
    """Manages the LLM ↔ TTS loop for a single conversation turn.

    When a final STT transcript arrives, the handler:
    1. Calls Mimo v2.5 to generate a response.
    2. Sends the response text through ElevenLabs TTS.
    3. Converts TTS output to PCM s16le mono 16 kHz.
    4. Sends the PCM chunks back over the bridge.
    """

    def __init__(
        self,
        llm: MimoLLMClient,
        tts: ElevenLabsTTSClient,
        bridge: ClientConnection,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        self.llm = llm
        self.tts = tts
        self.bridge = bridge
        self.system_prompt = system_prompt
        self.chunk_bytes = chunk_bytes
        self.conversation_history: list[dict[str, str]] = []
        self.current_call_id: str | None = None
        self._tts_lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()
        self._session_valid: Callable[[], bool] | None = None

    def cancel(self) -> None:
        """Cancel any in-progress TTS streaming."""
        self._cancel_event.set()

    def reset(self) -> None:
        """Reset conversation state for a new call."""
        self.conversation_history.clear()
        self.current_call_id = None
        self._cancel_event.clear()

    async def on_final_transcript(self, text: str) -> None:
        """Handle a committed/final STT transcript."""
        if not text.strip():
            return
        LOG.info("final transcript: %s", text)
        self.conversation_history.append({"role": "user", "content": text})

        try:
            await self._generate_and_speak()
        except (LLMError, TTSError, Exception) as exc:
            LOG.error("conversation turn failed: %s", exc)
            # Send a brief apology so the caller knows the agent is alive
            await self._speak_text("Sorry, I encountered an error. Please try again.")

    async def _generate_and_speak(self) -> None:
        """LLM → TTS → PCM → bridge."""
        # Build messages with rolling context (last 20 turns max)
        max_turns = 40  # 20 user + 20 assistant
        history = self.conversation_history[-max_turns:]

        response_text = await self.llm.complete(
            history,
            system_prompt=self.system_prompt,
        )
        if not response_text.strip():
            LOG.warning("LLM returned empty response")
            return

        LOG.info("LLM response: %s", response_text[:200])
        self.conversation_history.append({"role": "assistant", "content": response_text})

        await self._speak_text(response_text)

    async def _speak_text(self, text: str) -> None:
        """TTS → PCM → send only while this call session remains valid."""
        call_id = self.current_call_id
        if not call_id or (self._session_valid and not self._session_valid()):
            LOG.info("skip TTS for invalid call session=%s", call_id)
            return
        async with self._tts_lock:
            self._cancel_event.clear()
            if self._session_valid and not self._session_valid():
                return
            try:
                await self.bridge.send(AudioPlaying(call_id).to_json())
            except Exception:
                LOG.debug("could not send audio_playing notification")

            try:
                # ElevenLabs currently returns audio/mpeg for this endpoint
                # despite the requested PCM output format. Accumulate the
                # complete container before decoding; decoding each network
                # chunk independently turns MP3 fragments into crackle.
                compressed = bytearray()
                async for audio_chunk in self.tts.synthesize_streaming(text):
                    if self._cancel_event.is_set():
                        LOG.info("TTS streaming cancelled")
                        break
                    compressed.extend(audio_chunk)
                if not self._cancel_event.is_set() and compressed:
                    pcm = wav_to_pcm_s16le_16k(bytes(compressed))
                    LOG.info("TTS decoded: container_bytes=%d pcm_bytes=%d rms=%.1f", len(compressed), len(pcm), pcm_rms_s16le(pcm))
                    for frame in chunk_pcm(pcm, self.chunk_bytes):
                        if self._cancel_event.is_set() or (self._session_valid and not self._session_valid()):
                            LOG.info("stop outbound TTS for stale call session=%s", call_id)
                            break
                        await self.bridge.send(pack_audio_frame(call_id, frame))
            except TTSError as exc:
                LOG.error("TTS error: %s", exc)
            except Exception as exc:
                LOG.error("TTS unexpected error: %s", exc)

            try:
                if not self._session_valid or self._session_valid():
                    await self.bridge.send(AudioDone(call_id).to_json())
            except Exception:
                LOG.debug("could not send audio_done notification")


class MEOWcallerAgent:
    """Full conversational bridge client."""

    def __init__(self, args: argparse.Namespace, api_key: str) -> None:
        self.args = args
        self.api_key = api_key
        self.bridge: ClientConnection | Any | None = None
        self.stt: RealtimeSTT | None = None
        self.stt_task: asyncio.Task[None] | None = None
        self.conversation: ConversationHandler | None = None
        self.current_call_id: str | None = None
        self._session_generation = 0
        self.stop_event = asyncio.Event()

        # Build LLM client from environment
        self.llm = MimoLLMClient(
            base_url=os.getenv("MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
            api_key=os.getenv("MIMO_API_KEY", ""),
            model=os.getenv("MIMO_MODEL", "mimo-v2.5"),
        )

        # Build TTS client from environment
        # Voice settings match existing MEOWcaller WAV announcement:
        # Kira (gmnazjXOFoOcWA59sd5m), speed 0.78, stability 0.5, etc.
        self.tts = ElevenLabsTTSClient(
            api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            voice_id=os.getenv("ELEVENLABS_TTS_VOICE_ID", ElevenLabsTTSClient.DEFAULT_VOICE_ID),
            model_id=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"),
            speed=float(os.getenv("ELEVENLABS_TTS_SPEED", "0.94")),
            stability=float(os.getenv("ELEVENLABS_TTS_STABILITY", "0.5")),
            similarity_boost=float(os.getenv("ELEVENLABS_TTS_SIMILARITY_BOOST", "0.5")),
            style=float(os.getenv("ELEVENLABS_TTS_STYLE", "0.5")),
        )

    async def connect_bridge(self) -> None:
        LOG.info("connecting to MEOWcaller bridge: %s", self.args.bridge_url)
        self.bridge = await connect(
            self.args.bridge_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=self.args.connect_timeout,
        )
        hello = build_agent_hello(
            self.args.agent_id,
            capabilities={"stt": "elevenlabs_scribe_v2_realtime", "tts": "elevenlabs_streaming"},
        )
        await self.bridge.send(hello)
        LOG.info("connected to MEOWcaller bridge; waiting for call_started")

    async def run(self) -> None:
        await self.connect_bridge()
        if self.bridge is None:
            raise AgentError("bridge WebSocket is not connected")
        try:
            async for message in self.bridge:
                if isinstance(message, bytes):
                    try:
                        frame_call_id, pcm = unpack_audio_frame(message)
                    except ValueError as exc:
                        LOG.warning("dropping invalid inbound audio frame: %s", exc)
                        continue
                    if frame_call_id != self.current_call_id:
                        LOG.warning(
                            "dropping stale inbound audio: frame=%s current=%s",
                            frame_call_id,
                            self.current_call_id,
                        )
                        continue
                    await self.forward_audio(pcm)
                else:
                    await self.handle_control(message)
        except ConnectionClosed as exc:
            if not self.stop_event.is_set():
                raise AgentError(f"MEOWcaller bridge closed: {exc}") from exc
        finally:
            await self.stop()

    async def handle_control(self, raw: str) -> None:
        event = parse_bridge_event(raw)
        if event is None:
            return
        event_type = classify_bridge_event(event)

        if event_type == "call_started":
            await self.start_call(event)
        elif event_type == "call_ended":
            ended_call_id = str(event.get("call_id", ""))
            if ended_call_id and ended_call_id != self.current_call_id:
                LOG.info("ignore stale call_ended=%s current=%s", ended_call_id, self.current_call_id)
                return
            LOG.info("call ended: %s", ended_call_id or self.current_call_id or "unknown")
            await self.stop_call(flush=True)
        elif event_type == "cancel":
            cancel_call_id = str(event.get("call_id", ""))
            if cancel_call_id and cancel_call_id != self.current_call_id:
                LOG.warning("ignore stale cancel=%s current=%s", cancel_call_id, self.current_call_id)
                return
            LOG.info("call cancelled: %s", cancel_call_id or self.current_call_id or "unknown")
            if self.conversation:
                self.conversation.cancel()
            await self.stop_call()
        elif event_type == "disconnect":
            LOG.info("bridge disconnect")
            self.stop_event.set()
        elif event_type == "error":
            LOG.error("MEOWcaller bridge error: %s", event)
        else:
            LOG.debug("bridge control event: %s", event)

    async def start_call(self, call: dict[str, Any]) -> None:
        await self.stop_call()
        self._session_generation += 1
        generation = self._session_generation
        self.current_call_id = str(call.get("call_id", "unknown"))
        LOG.info(
            "call started: call_id=%s caller_id=%s",
            self.current_call_id,
            call.get("caller_id", call.get("remote_jid", "unknown")),
        )

        # Start STT
        self.stt = RealtimeSTT(self.api_key, self.args)
        self.stt._call_id = self.current_call_id  # type: ignore[attr-defined]
        await self.stt.connect()
        self.stt_task = asyncio.create_task(self.stt.receive_events(), name="elevenlabs-stt-receiver")

        try:
            await asyncio.wait_for(self.stt.ready.wait(), timeout=self.args.session_timeout)
        except asyncio.TimeoutError as exc:
            await self.stop_call()
            raise AgentError("ElevenLabs STT did not send session_started") from exc

        # Create conversation handler
        self.conversation = ConversationHandler(
            self.llm,
            self.tts,
            self.bridge,
            system_prompt=self.args.system_prompt or SYSTEM_PROMPT,
        )
        self.conversation.current_call_id = self.current_call_id
        self.conversation._session_valid = lambda: (
            self._session_generation == generation
            and self.current_call_id == call.get("call_id")
            and self.conversation is not None
        )

    async def forward_audio(self, pcm: bytes) -> None:
        if self.stt is None or self.stt.closed:
            LOG.debug("dropping %d audio bytes: no active STT session", len(pcm))
            return
        await self.stt.send_pcm(pcm)

    async def on_stt_final(self, text: str, call_id: str | None = None) -> None:
        """Handle a final only if it belongs to the active call session."""
        if call_id and call_id != self.current_call_id:
            LOG.info("ignore stale STT final call=%s current=%s", call_id, self.current_call_id)
            return
        if self.conversation is None or self.conversation.current_call_id != self.current_call_id:
            return
        await self.conversation.on_final_transcript(text)

    async def stop_call(self, *, flush: bool = False) -> None:
        stt = self.stt
        conversation = self.conversation
        call_id = self.current_call_id
        # Invalidate this session immediately. A late STT final or old TTS
        # task must not target the next call.
        self._session_generation += 1
        self.current_call_id = None
        self.conversation = None
        if flush and stt is not None:
            await stt.commit()
            await asyncio.sleep(0.15)
        if conversation:
            conversation.cancel()
            conversation.reset()
        await self._stop_stt()
        LOG.info("stopped call session=%s", call_id)

    async def _stop_stt(self) -> None:
        if self.stt is not None:
            await self.stt.close()
        if self.stt_task is not None:
            self.stt_task.cancel()
            try:
                await self.stt_task
            except (asyncio.CancelledError, AgentError):
                pass
        self.stt = None
        self.stt_task = None

    async def stop(self) -> None:
        self.stop_event.set()
        await self.stop_call()
        if self.bridge is not None:
            await self.bridge.close()
            self.bridge = None


# ---------------------------------------------------------------------------
# stdout monitor: reads transcript_final lines and triggers conversation
# ---------------------------------------------------------------------------

async def _monitor_stt_output(agent: MEOWcallerAgent) -> None:
    """Read JSON lines from the STT receiver task and forward finals."""
    # The STT task prints to stdout; we intercept by monkey-patching
    # or by reading from a pipe.  Instead, we override the STT's
    # handle_event to also call on_stt_final directly.
    pass


# We override RealtimeSTT.handle_event to also notify the agent.
_orig_handle_event = RealtimeSTT.handle_event


def _patched_handle_event(self: RealtimeSTT, event: dict[str, Any]) -> None:
    """Extended handler that also forwards finals to the conversation."""
    _orig_handle_event(self, event)
    message_type = event.get("message_type", event.get("type", "unknown"))
    if message_type in {
        "committed_transcript",
        "committed_transcript_with_timestamps",
        "final_transcript",
        "final_transcript_with_timestamps",
    }:
        text = event.get("text", "")
        if text and self._agent_ref is not None:
            # ElevenLabs may emit the same committed text in more than one
            # event variant; forward each final segment only once and retain
            # the owning call_id.
            if text == getattr(self, "_agent_forwarded_text", None):
                return
            self._agent_forwarded_text = text
            call_id = getattr(self, "_call_id", None)
            loop = asyncio.get_running_loop()
            loop.create_task(self._agent_ref.on_stt_final(text, call_id))


RealtimeSTT.handle_event = _patched_handle_event  # type: ignore[assignment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MEOWcaller conversational agent (STT + LLM + TTS)"
    )
    parser.add_argument(
        "--bridge-url",
        default=os.getenv("MEOWCALLER_BRIDGE_URL", "ws://127.0.0.1:9090/ws"),
    )
    parser.add_argument(
        "--elevenlabs-url",
        default=os.getenv("ELEVENLABS_STT_URL", "wss://api.elevenlabs.io/v1/speech-to-text/realtime"),
    )
    parser.add_argument(
        "--agent-id",
        default=os.getenv("MEOWCALLER_AGENT_ID", "converse-agent-001"),
    )
    parser.add_argument(
        "--model-id",
        default=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2_realtime"),
    )
    parser.add_argument(
        "--language-code",
        default=os.getenv("ELEVENLABS_STT_LANGUAGE", "id"),
    )
    parser.add_argument("--connect-timeout", type=float, default=15.0)
    parser.add_argument("--session-timeout", type=float, default=15.0)
    parser.add_argument("--log-level", default=os.getenv("MEOWCALLER_AGENT_LOG_LEVEL", "INFO"))
    parser.add_argument("--system-prompt", default=os.getenv("MEOWCALLER_SYSTEM_PROMPT", ""))
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise AgentError("ELEVENLABS_API_KEY is not set")
    if not os.getenv("MIMO_API_KEY", "").strip():
        raise AgentError("MIMO_API_KEY is not set")

    agent = MEOWcallerAgent(args, api_key)

    # Patch STT to carry agent reference
    async def _patch_stt_connect(orig_stt: RealtimeSTT) -> None:
        await orig_stt.connect()
        orig_stt._agent_ref = agent  # type: ignore[attr-defined]

    # Monkey-patch start_call to inject agent ref
    _orig_start_call = agent.start_call

    async def _patched_start_call(call: dict[str, Any]) -> None:
        await _orig_start_call(call)
        if agent.stt is not None:
            agent.stt._agent_ref = agent  # type: ignore[attr-defined]

    agent.start_call = _patched_start_call  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, agent.stop_event.set)
        except NotImplementedError:
            pass

    runner = asyncio.create_task(agent.run(), name="meowcaller-bridge-runner")
    stopper = asyncio.create_task(agent.stop_event.wait(), name="agent-stop-waiter")
    done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    if stopper in done and not runner.done():
        await agent.stop()
        await runner
    else:
        stopper.cancel()
        try:
            await stopper
        except asyncio.CancelledError:
            pass
        await agent.stop()
        await runner
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130
    except (AgentError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
