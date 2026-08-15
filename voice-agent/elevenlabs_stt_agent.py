#!/usr/bin/env python3
"""Minimal MEOWcaller WebSocket -> ElevenLabs Realtime STT agent.

The script intentionally handles STT only. It connects to the local
MEOWcaller audio bridge, receives JSON control messages and binary PCM audio,
then forwards the audio to ElevenLabs Scribe Realtime over a second WebSocket.

No API key is stored in this file. The launcher supplies ELEVENLABS_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import sys
from typing import Any
from urllib.parse import urlencode

try:
    from websockets.asyncio.client import ClientConnection, connect
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - compatibility with older websockets
    from websockets import connect  # type: ignore
    from websockets.legacy.client import WebSocketClientProtocol as ClientConnection  # type: ignore
    from websockets.exceptions import ConnectionClosed  # type: ignore

LOG = logging.getLogger("meowcaller-stt-agent")


class AgentError(RuntimeError):
    """Expected agent configuration/protocol error."""


class RealtimeSTT:
    """Small wrapper around the ElevenLabs Scribe Realtime WebSocket."""

    def __init__(self, api_key: str, args: argparse.Namespace) -> None:
        self.api_key = api_key
        self.args = args
        self.ws: ClientConnection | Any | None = None
        self.ready = asyncio.Event()
        self.closed = False

    def url(self) -> str:
        params = {
            "model_id": self.args.model_id,
            "audio_format": "pcm_16000",
            "language_code": self.args.language_code,
            "commit_strategy": "vad",
            "include_timestamps": "false",
            "include_language_detection": "true",
        }
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
            if not self.closed:
                raise AgentError(f"ElevenLabs STT connection closed: {exc}") from exc

    def handle_event(self, event: dict[str, Any]) -> None:
        message_type = event.get("message_type", event.get("type", "unknown"))
        if message_type == "session_started":
            config = event.get("config", {})
            LOG.info("STT session started: %s", event.get("session_id", "unknown"))
            LOG.debug("STT negotiated config: %s", config)
            self.ready.set()
        elif message_type in {"partial_transcript", "partial"}:
            text = event.get("text", "")
            if text:
                print(json.dumps({"type": "transcript_partial", "text": text}, ensure_ascii=False), flush=True)
        elif message_type in {
            "committed_transcript",
            "committed_transcript_with_timestamps",
            "final_transcript",
            "final_transcript_with_timestamps",
        }:
            text = event.get("text", "")
            if text:
                print(json.dumps({"type": "transcript_final", "text": text}, ensure_ascii=False), flush=True)
        elif message_type in {"error", "auth_error", "quota_exceeded", "rate_limited", "commit_throttled", "input_error", "invalid_request"}:
            LOG.error("ElevenLabs STT error (%s): %s", message_type, event.get("error", event))
        else:
            LOG.debug("STT event: %s", event)

    async def send_pcm(self, pcm: bytes) -> None:
        if self.ws is None or self.closed:
            return
        if not pcm:
            return
        message = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "commit": False,
            "sample_rate": 16000,
        }
        await self.ws.send(json.dumps(message, separators=(",", ":")))

    async def close(self) -> None:
        self.closed = True
        self.ready.clear()
        if self.ws is not None:
            await self.ws.close()
            self.ws = None


class MEOWcallerAgent:
    """Single-agent/single-call bridge client."""

    def __init__(self, args: argparse.Namespace, api_key: str) -> None:
        self.args = args
        self.api_key = api_key
        self.bridge: ClientConnection | Any | None = None
        self.stt: RealtimeSTT | None = None
        self.stt_task: asyncio.Task[None] | None = None
        self.current_call_id: str | None = None
        self.stop_event = asyncio.Event()

    async def connect_bridge(self) -> None:
        LOG.info("connecting to MEOWcaller bridge: %s", self.args.bridge_url)
        self.bridge = await connect(
            self.args.bridge_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=self.args.connect_timeout,
        )
        await self.bridge.send(json.dumps({
            "type": "agent_hello",
            "agent_id": self.args.agent_id,
            "protocol_version": 1,
            "capabilities": {"stt": "elevenlabs_scribe_v2_realtime"},
        }, separators=(",", ":")))
        LOG.info("connected to MEOWcaller bridge; waiting for call_started")

    async def run(self) -> None:
        await self.connect_bridge()
        if self.bridge is None:
            raise AgentError("bridge WebSocket is not connected")
        try:
            async for message in self.bridge:
                if isinstance(message, bytes):
                    await self.forward_audio(message)
                else:
                    await self.handle_control(message)
        except ConnectionClosed as exc:
            if not self.stop_event.is_set():
                raise AgentError(f"MEOWcaller bridge closed: {exc}") from exc
        finally:
            await self.stop_stt()

    async def handle_control(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            LOG.warning("ignoring non-JSON bridge message")
            return
        event_type = event.get("type", event.get("event", ""))
        if event_type in {"call_started", "call_start"}:
            await self.start_stt(event)
        elif event_type in {"call_ended", "call_end"}:
            LOG.info("call ended: %s", event.get("call_id", self.current_call_id or "unknown"))
            await self.stop_stt()
            self.current_call_id = None
        elif event_type == "error":
            LOG.error("MEOWcaller bridge error: %s", event)
        else:
            LOG.debug("bridge control event: %s", event)

    async def start_stt(self, call: dict[str, Any]) -> None:
        await self.stop_stt()
        self.current_call_id = str(call.get("call_id", "unknown"))
        LOG.info("call started: call_id=%s caller_id=%s", self.current_call_id, call.get("caller_id", call.get("remote_jid", "unknown")))
        self.stt = RealtimeSTT(self.api_key, self.args)
        await self.stt.connect()
        self.stt_task = asyncio.create_task(self.stt.receive_events(), name="elevenlabs-stt-receiver")
        try:
            await asyncio.wait_for(self.stt.ready.wait(), timeout=self.args.session_timeout)
        except asyncio.TimeoutError as exc:
            await self.stop_stt()
            raise AgentError("ElevenLabs STT did not send session_started") from exc

    async def forward_audio(self, pcm: bytes) -> None:
        if self.stt is None or self.stt.closed:
            LOG.debug("dropping %d audio bytes: no active STT session", len(pcm))
            return
        await self.stt.send_pcm(pcm)

    async def stop_stt(self) -> None:
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

    async def close(self) -> None:
        self.stop_event.set()
        await self.stop_stt()
        if self.bridge is not None:
            await self.bridge.close()
            self.bridge = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MEOWcaller PCM to ElevenLabs Realtime STT agent")
    parser.add_argument("--bridge-url", default=os.getenv("MEOWCALLER_BRIDGE_URL", "ws://127.0.0.1:8765/ws"))
    parser.add_argument("--elevenlabs-url", default=os.getenv("ELEVENLABS_STT_URL", "wss://api.elevenlabs.io/v1/speech-to-text/realtime"))
    parser.add_argument("--agent-id", default=os.getenv("MEOWCALLER_AGENT_ID", "stt-agent-001"))
    parser.add_argument("--model-id", default=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2_realtime"))
    parser.add_argument("--language-code", default=os.getenv("ELEVENLABS_STT_LANGUAGE", "id"))
    parser.add_argument("--connect-timeout", type=float, default=15.0)
    parser.add_argument("--session-timeout", type=float, default=15.0)
    parser.add_argument("--log-level", default=os.getenv("MEOWCALLER_AGENT_LOG_LEVEL", "INFO"))
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise AgentError("ELEVENLABS_API_KEY is not set")
    agent = MEOWcallerAgent(args, api_key)
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
        await agent.close()
        await runner
    else:
        # The bridge task ended (normally or with an error); do not wait
        # forever for a signal task that is still pending.
        stopper.cancel()
        try:
            await stopper
        except asyncio.CancelledError:
            pass
        await agent.close()
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
