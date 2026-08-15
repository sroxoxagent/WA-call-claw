"""OpenClaw Gateway WebSocket client for MEOWcaller voice path.

Connects to the local OpenClaw gateway (ws://127.0.0.1:18789) and provides
a simple interface to send messages to agent sessions and receive responses.

Protocol reference (from OpenClaw source):
  - Frame types: req, res, event
  - First frame must be: { type:"req", id:"1", method:"connect", params:{...} }
  - Response: { type:"res", id:"1", ok:true, payload:{ type:"hello-ok", ... } }
  - Chat send: { type:"req", id:"N", method:"chat.send", params:{ sessionKey, message, idempotencyKey } }
  - Chat events: { type:"event", event:"chat", payload:{ runId, sessionKey, seq, state, message } }
  - state: "delta" (streaming) | "final" (complete) | "error" | "aborted"

No API keys are stored in this file. Keys are passed via environment
variables or constructor arguments.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

from cryptography.hazmat.primitives import serialization

try:
    from websockets.asyncio.client import ClientConnection, connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    from websockets import connect  # type: ignore
    from websockets.legacy.client import WebSocketClientProtocol as ClientConnection  # type: ignore
    from websockets.exceptions import ConnectionClosed  # type: ignore

LOG = logging.getLogger("meowcaller.gateway")

# Gateway defaults
DEFAULT_GATEWAY_URL = "ws://127.0.0.1:18789"
DEFAULT_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
RECONNECT_RETRY_DELAY = 5.0  # detik antar percobaan reconnect background
DEFAULT_DEVICE_IDENTITY_PATH = os.getenv(
    "OPENCLAW_DEVICE_IDENTITY_PATH",
    str(Path.home() / ".openclaw" / "identity" / "device.json"),
)
# OpenClaw gateway protocol negotiation: advertise a RANGE so the same client
# works against v3 gateways (2026.3.x) and v4 gateways (2026.5+).
#   - v3 server accepts iff maxProtocol >= 3 and minProtocol <= 3
#   - v4 server accepts iff maxProtocol >= 4 and minProtocol <= 4
#   - v4 servers reserve N-1 (v3) for role=node / mode=probe ONLY — general
#     operator/backend clients MUST advertise up to v4 or they get
#     "protocol mismatch" (close 1002).
# The negotiated version comes back in hello-ok payload.protocol.
MIN_PROTOCOL_VERSION = 3
MAX_PROTOCOL_VERSION = 4
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_OPERATOR_SCOPES = ["operator.read", "operator.write"]


class GatewayError(RuntimeError):
    """Gateway connection or protocol error."""


class GatewayAuthError(GatewayError):
    """Gateway authentication failed."""


class GatewayTimeoutError(GatewayError):
    """Gateway request timed out."""


class OpenClawGatewayClient:
    """Async WebSocket client for the OpenClaw gateway.

    Usage:
        client = OpenClawGatewayClient(token="my-token")
        await client.connect()
        async for event in client.send_chat("session-key", "Hello"):
            print(event)
        await client.close()
    """

    def __init__(
        self,
        url: str = DEFAULT_GATEWAY_URL,
        token: str = DEFAULT_GATEWAY_TOKEN,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        device_identity_path: str = DEFAULT_DEVICE_IDENTITY_PATH,
        # The Gateway sends its own application-level tick events. Do not
        # inject client-side ping/pong by default: the local Gateway accepts
        # long-lived idle sockets reliably, while custom pings can produce
        # false keepalive timeouts in this backend client.
        heartbeat_interval: float = 0.0,
    ) -> None:
        self.url = url
        self.token = token
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.device_identity_path = device_identity_path
        self.heartbeat_interval = heartbeat_interval
        self.ws: ClientConnection | Any | None = None
        self.connected = False
        self._conn_id: str | None = None
        self.protocol_version: int | None = None
        self._connected_at: float = 0.0
        self._req_counter = 0
        self._pending: dict[str, Any] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_lock = asyncio.Lock()

    def _next_id(self) -> str:
        self._req_counter += 1
        return f"mc-{self._req_counter}"

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _start_heartbeat(self) -> None:
        """Start a periodic WebSocket ping/pong heartbeat task.

        The heartbeat runs while the connection is idle, sending application-
        level pings to verify the gateway is responsive.  On pong timeout the
        task stops and sets ``self.connected = False`` so the caller (main
        loop) can trigger reconnect.

        Safe to call multiple times — cancels any prior heartbeat task first.
        """
        await self._cancel_heartbeat()
        if self.heartbeat_interval <= 0:
            return
        LOG.info("gateway heartbeat started (interval=%gs)", self.heartbeat_interval)
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="gateway-heartbeat"
        )

    async def _cancel_heartbeat(self) -> None:
        """Cancel the heartbeat task if running and wait for it to finish."""
        task = self._heartbeat_task
        if task is not None and task is asyncio.current_task():
            # A heartbeat-triggered reconnect must not cancel itself.
            self._heartbeat_task = None
            return
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected when cancelling a task
            except Exception:
                pass  # heartbeat exceptions are non-fatal
        self._heartbeat_task = None

    async def _background_reconnect(self) -> None:
        """Restore the persistent gateway connection without waiting for a call.

        Retries indefinitely (every RETRY_DELAY seconds) until the connection
        is restored — the gateway may be down for longer than the old fixed
        4-attempt window (~8s), and the agent must come back on its own
        instead of waiting for the next call to trigger a lazy reconnect.
        """
        attempt = 0
        while True:
            if attempt:
                await asyncio.sleep(RECONNECT_RETRY_DELAY)
            attempt += 1
            try:
                await self.reconnect()
                LOG.info("gateway background reconnect succeeded (attempt %d)", attempt)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Log the first few attempts, then once per minute to avoid spam.
                if attempt <= 3 or attempt % 12 == 0:
                    LOG.warning(
                        "gateway background reconnect failed (attempt %d): %s",
                        attempt,
                        exc,
                    )

    async def _heartbeat_loop(self) -> None:
        """Periodically ping the gateway and restore a stale connection.

        Pings every ``heartbeat_interval`` seconds; a successful round-trip is
        logged once per minute so the logs can prove the WebSocket layer stayed
        alive even when an application-level request stalls.
        """
        cycle = 0
        while self.connected and self.ws is not None:
            await asyncio.sleep(self.heartbeat_interval)
            # Re-check after sleep — task may have been cancelled or ws closed
            if not self.connected or self.ws is None:
                break
            cycle += 1
            try:
                t0 = time.monotonic()
                pong_waiter = await self.ws.ping()
                # Pong timeout must be generous: observed pongs that normally
                # arrive in <1ms occasionally stall >5s (Recv-Q backlog on the
                # client socket), which false-triggered a reconnect loop with
                # a 5s timeout. Keep the ping cadence tight, the verdict loose.
                pong_timeout = max(self.heartbeat_interval * 3.0, 15.0)
                await asyncio.wait_for(pong_waiter, timeout=pong_timeout)
                rtt_ms = (time.monotonic() - t0) * 1000.0
                if cycle % 12 == 0:
                    LOG.info(
                        "gateway heartbeat ok (rtt=%.1fms, interval=%gs)",
                        rtt_ms,
                        self.heartbeat_interval,
                    )
            except (asyncio.TimeoutError, ConnectionClosed, OSError) as exc:
                LOG.warning("gateway heartbeat failed (%s), scheduling reconnect", exc)
                self.connected = False
                task = self._reconnect_task
                if task is None or task.done():
                    self._reconnect_task = asyncio.create_task(
                        self._background_reconnect(), name="gateway-reconnect"
                    )
                break

    @staticmethod
    def _base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def _load_device_identity(self) -> dict[str, str]:
        try:
            identity = json.loads(Path(self.device_identity_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayError(
                f"device identity unavailable at {self.device_identity_path}"
            ) from exc
        required = ("deviceId", "publicKeyPem", "privateKeyPem")
        if not all(isinstance(identity.get(key), str) and identity[key] for key in required):
            raise GatewayError("device identity is incomplete")
        return identity

    async def _wait_connect_challenge(self, timeout: float) -> str:
        """Read the gateway's nonce challenge before signing the handshake."""
        if self.ws is None:
            raise GatewayError("gateway websocket is not open")
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise GatewayTimeoutError("gateway connect challenge timed out") from exc
        except Exception as exc:
            raise GatewayError("connection lost waiting for gateway challenge") from exc
        if isinstance(raw, bytes):
            raise GatewayError("gateway connect challenge was not JSON")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayError("gateway connect challenge was invalid JSON") from exc
        if frame.get("type") != "event" or frame.get("event") != "connect.challenge":
            raise GatewayError("gateway connect challenge missing")
        nonce = frame.get("payload", {}).get("nonce")
        if not isinstance(nonce, str) or not nonce.strip():
            raise GatewayError("gateway connect challenge missing nonce")
        return nonce.strip()

    async def connect(self) -> None:
        """Connect to the gateway and perform the handshake."""
        LOG.info("connecting to gateway: %s", self.url)
        self.ws = await connect(
            self.url,
            max_size=None,
            # Disable websockets' built-in keepalive: the client heartbeat
            # below is the single owner of ping/pong traffic. Two concurrent
            # keepalive loops caused false ping timeouts on the Gateway.
            ping_interval=None,
            ping_timeout=None,
            close_timeout=1.0,
            open_timeout=self.connect_timeout,
        )

        # The gateway sends a nonce challenge before the connect request.
        nonce = await self._wait_connect_challenge(timeout=self.connect_timeout)
        identity = self._load_device_identity()
        scopes = list(DEFAULT_OPERATOR_SCOPES)
        signed_at_ms = int(time.time() * 1000)
        client_id = "gateway-client"
        client_mode = "backend"
        role = "operator"
        signature_token = self.token or ""
        payload = "|".join(
            [
                "v3",
                identity["deviceId"],
                client_id,
                client_mode,
                role,
                ",".join(scopes),
                str(signed_at_ms),
                signature_token,
                nonce,
                "linux",
                "",
            ]
        )
        private_key = serialization.load_pem_private_key(
            identity["privateKeyPem"].encode(), password=None
        )
        signature = private_key.sign(payload.encode())
        public_key = serialization.load_pem_public_key(identity["publicKeyPem"].encode())
        public_key_raw = public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

        connect_params = {
            "minProtocol": MIN_PROTOCOL_VERSION,
            "maxProtocol": MAX_PROTOCOL_VERSION,
            "client": {
                "id": client_id,
                "displayName": "MEOWcaller Voice Agent",
                "version": "1.0.0",
                "platform": "linux",
                "mode": client_mode,
            },
            "caps": [],
            "role": role,
            "scopes": scopes,
            "device": {
                "id": identity["deviceId"],
                "publicKey": self._base64url(public_key_raw),
                "signature": self._base64url(signature),
                "signedAt": signed_at_ms,
                "nonce": nonce,
            },
        }
        if self.token:
            connect_params["auth"] = {"token": self.token}

        connect_id = self._next_id()
        connect_frame = {
            "type": "req",
            "id": connect_id,
            "method": "connect",
            "params": connect_params,
        }

        await self.ws.send(json.dumps(connect_frame, separators=(",", ":")))

        # Wait for hello-ok response
        try:
            raw = await self._wait_response(connect_id)
        except GatewayTimeoutError as exc:
            await self._close_ws()
            raise GatewayError("gateway handshake timed out") from exc

        if not raw.get("ok"):
            error = raw.get("error", {})
            error_msg = error.get("message", "unknown error")
            await self._close_ws()
            error_lower = error_msg.lower()
            if (
                "auth" in error_lower
                or "unauthorized" in error_lower
                or "token" in error_lower
                or "pairing" in error_lower
            ):
                raise GatewayAuthError(f"gateway auth failed: {error_msg}")
            raise GatewayError(f"gateway connect failed: {error_msg}")

        payload = raw.get("payload", {})
        self._conn_id = payload.get("server", {}).get("connId", "unknown")
        self.protocol_version = payload.get("protocol", MAX_PROTOCOL_VERSION)
        self._connected_at = time.monotonic()
        self.connected = True
        LOG.info(
            "gateway connected: connId=%s protocol=v%s (advertised %d-%d)",
            self._conn_id,
            self.protocol_version,
            MIN_PROTOCOL_VERSION,
            MAX_PROTOCOL_VERSION,
        )
        await self._start_heartbeat()

    async def send_agent(
        self,
        session_key: str,
        message: str,
        *,
        extra_system_prompt: str = "",
        thinking: str | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send one agent turn with an explicit per-turn system context.

        Uses the gateway ``agent`` RPC. On connection drop, automatically
        reconnects and retries once. The session key remains isolated per
        call, while ``extraSystemPrompt`` carries the allowlisted MD profile
        and selected session memory safely as system context.
        """
        try:
            async for event in self._send_agent_once(
                session_key, message,
                extra_system_prompt=extra_system_prompt,
                thinking=thinking,
                timeout=timeout,
            ):
                yield event
        except (GatewayError, ConnectionClosed) as exc:
            if "not connected" in str(exc).lower() or "timed out" in str(exc).lower():
                raise
            LOG.warning("agent send failed (%s), reconnecting and retrying...", exc)
            await self.reconnect()
            async for event in self._send_agent_once(
                session_key, message,
                extra_system_prompt=extra_system_prompt,
                thinking=thinking,
                timeout=timeout,
            ):
                yield event

    async def _send_agent_once(
        self,
        session_key: str,
        message: str,
        *,
        extra_system_prompt: str = "",
        thinking: str | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Single attempt for send_agent (no retry)."""
        if not self.connected or self.ws is None:
            raise GatewayError("not connected to gateway")

        timeout = timeout or self.request_timeout
        req_id = self._next_id()
        idempotency_key = f"meowcaller-agent-{req_id}-{int(time.time() * 1000)}"
        params: dict[str, Any] = {
            "sessionKey": session_key,
            "message": message,
            "idempotencyKey": idempotency_key,
            "timeout": max(1, int(timeout)),
        }
        if extra_system_prompt.strip():
            params["extraSystemPrompt"] = extra_system_prompt
        if thinking:
            params["thinking"] = thinking

        await self.ws.send(json.dumps({
            "type": "req",
            "id": req_id,
            "method": "agent",
            "params": params,
        }, separators=(",", ":")))

        try:
            first = await self._wait_response(req_id, timeout=timeout)
        except GatewayTimeoutError as exc:
            raise GatewayTimeoutError(f"agent ack timed out after {timeout}s") from exc
        if not first.get("ok"):
            error = first.get("error", {})
            raise GatewayError(f"agent rejected: {error.get('message', 'unknown')}")

        first_payload = first.get("payload", {})
        run_id = first_payload.get("runId", req_id)
        yield {"type": "ack", "runId": run_id, "status": first_payload.get("status", "accepted")}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                frame = await self._wait_response(req_id, timeout=remaining)
            except GatewayTimeoutError:
                raise GatewayTimeoutError(f"agent result timed out after {timeout}s")
            if not frame.get("ok"):
                error = frame.get("error", {})
                raise GatewayError(f"agent failed: {error.get('message', 'unknown')}")
            payload = frame.get("payload", {})
            status = payload.get("status", "")
            if status in {"accepted", "started", "in_flight"}:
                continue

            result = payload.get("result", {})
            response_text = ""
            for item in result.get("payloads", []) if isinstance(result, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    response_text += item["text"]
            yield {
                "type": "final" if status in {"ok", "completed"} else status or "final",
                "runId": run_id,
                "state": "final" if status in {"ok", "completed"} else status or "final",
                "message": response_text,
            }
            return

        raise GatewayTimeoutError(f"agent result timed out after {timeout}s")

    async def send_chat(
        self,
        session_key: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message to an agent session and yield response events.

        Yields dicts with keys:
          - type: "ack" | "delta" | "final" | "error" | "aborted"
          - For delta/final: runId, sessionKey, seq, state, message (content)
          - For error: runId, sessionKey, errorMessage

        The generator completes when state="final" or state="error"/"aborted".
        """
        if not self.connected or self.ws is None:
            raise GatewayError("not connected to gateway")

        # Long-idle Gateway WS sockets can go half-dead on the server side
        # (frames accepted into the TCP buffer but never dispatched), which
        # manifests as a silent ack timeout on chat.send. Refresh the
        # connection when it is older than 30s so every voice turn talks
        # over a fresh socket.
        age = time.monotonic() - self._connected_at
        if age > 30.0:
            LOG.info(
                "gateway connection %.0fs old — refreshing before chat.send",
                age,
            )
            await self.reconnect()

        timeout = timeout or self.request_timeout
        req_id = self._next_id()
        idempotency_key = f"meowcaller-{req_id}-{int(time.time() * 1000)}"

        chat_params = {
            "sessionKey": session_key,
            "message": message,
            "idempotencyKey": idempotency_key,
        }

        req_frame = {
            "type": "req",
            "id": req_id,
            "method": "chat.send",
            "params": chat_params,
        }

        await self.ws.send(json.dumps(req_frame, separators=(",", ":")))

        # The Gateway ack is emitted immediately. Keep this timeout short so
        # a stale socket is detected and reconnected before the voice turn
        # burns the full model-response timeout.
        ack_timeout = min(timeout, 25.0)
        ack = await self._send_and_wait_ack(req_frame, req_id, ack_timeout)

        if not ack.get("ok"):
            error = ack.get("error", {})
            raise GatewayError(f"chat.send rejected: {error.get('message', 'unknown')}")

        payload = ack.get("payload", {})
        run_id = payload.get("runId", req_id)
        status = payload.get("status", "unknown")
        LOG.info("chat.send ack: runId=%s status=%s", run_id, status)

        # Yield the ack
        yield {"type": "ack", "runId": run_id, "status": status}

        # Now listen for chat events until final/error/aborted
        deadline = time.monotonic() + timeout
        seq_seen = -1

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                raw_msg = await self._wait_event(timeout=min(remaining, 2.0))
            except GatewayTimeoutError:
                continue

            if raw_msg is None:
                continue

            # Check if this is a chat event for our run
            event_name = raw_msg.get("event", "")
            evt_payload = raw_msg.get("payload", {})

            if event_name != "chat":
                continue

            evt_run_id = evt_payload.get("runId", "")
            if evt_run_id and evt_run_id != run_id:
                continue

            state = evt_payload.get("state", "")
            evt_seq = evt_payload.get("seq", 0)

            # Some Gateway builds reuse seq=0 for the terminal event. Never
            # drop final/error/aborted states solely because their seq repeats.
            if state not in ("final", "error", "aborted"):
                if evt_seq <= seq_seen:
                    continue
                seq_seen = evt_seq

            # Extract message content
            message_content = ""
            msg_obj = evt_payload.get("message")
            if isinstance(msg_obj, dict):
                # Chat payloads use either a plain string or Pi-style content
                # blocks: [{"type":"text","text":"..."}].
                content = msg_obj.get("content", "")
                if isinstance(content, str):
                    message_content = content
                elif isinstance(content, list):
                    parts: list[str] = []
                    for block in content:
                        if isinstance(block, str):
                            parts.append(block)
                        elif isinstance(block, dict):
                            text = block.get("text", block.get("content", ""))
                            if isinstance(text, str):
                                parts.append(text)
                    message_content = "".join(parts)
                if not message_content:
                    message_content = msg_obj.get("text", "")
            elif isinstance(msg_obj, str):
                message_content = msg_obj

            yield {
                "type": state,
                "runId": evt_run_id,
                "sessionKey": evt_payload.get("sessionKey", session_key),
                "seq": evt_seq,
                "state": state,
                "message": message_content,
                "errorMessage": evt_payload.get("errorMessage", ""),
            }

            if state in ("final", "error", "aborted"):
                return

        # Timeout reached
        LOG.warning("chat.send event stream timed out after %ss", timeout)
        yield {"type": "timeout", "runId": run_id, "message": ""}

    async def _send_and_wait_ack(
        self, req_frame: dict[str, Any], req_id: str, ack_timeout: float
    ) -> dict[str, Any]:
        """Send a request frame and wait for its ack.

        Retries once with a fresh connection when the socket dies mid-send or
        mid-ack. GatewayTimeoutError is NOT retried: a silent gateway only gets
        slower when we pile on duplicate requests, and the caller already has
        a fallback for timeouts.
        """
        for attempt in (1, 2):
            try:
                if not self.connected or self.ws is None:
                    await self.reconnect()
                await self.ws.send(json.dumps(req_frame, separators=(",", ":")))
                return await self._wait_response(req_id, timeout=ack_timeout)
            except GatewayTimeoutError:
                # Ack timeout may mean a busy gateway OR a dead socket that
                # recv() cannot distinguish from silence. Probe the socket
                # before giving up; if the connection is gone, reconnect and
                # retry once instead of failing the whole voice turn.
                if attempt == 2:
                    raise
                if not await self._is_connection_alive():
                    LOG.warning(
                        "chat.send ack timed out and socket is dead — reconnecting and retrying"
                    )
                    await self.reconnect()
                    continue
                raise
            except (ConnectionClosed, OSError, GatewayError) as exc:
                if attempt == 2:
                    raise
                LOG.warning(
                    "chat.send lost connection on attempt %d: %s — reconnecting and retrying",
                    attempt,
                    exc,
                )
                await self.reconnect()
        raise GatewayError("chat.send retry exhausted")  # pragma: no cover

    async def _is_connection_alive(self) -> bool:
        """Probe the WebSocket with a ping; True if a pong comes back fast."""
        ws = self.ws
        if ws is None:
            return False
        try:
            pong = await ws.ping()
            await asyncio.wait_for(pong, timeout=3.0)
            return True
        except Exception:
            return False

    async def _wait_response(self, req_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """Wait for a response frame matching the given request ID."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayTimeoutError(f"response for {req_id} timed out")
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise GatewayTimeoutError(f"response for {req_id} timed out after {timeout}s")
            except Exception as exc:
                raise GatewayError("connection lost waiting for response") from exc
            if isinstance(raw, bytes):
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "res" and frame.get("id") == req_id:
                return frame
        raise GatewayTimeoutError(f"response for {req_id} timed out after {timeout}s")

    async def _wait_event(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """Wait for an event frame with a timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            except (ConnectionClosed, OSError) as exc:
                raise GatewayError("connection lost waiting for gateway event") from exc
            except Exception:
                return None
            if isinstance(raw, bytes):
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "event":
                return frame
        return None

    async def _close_ws(self) -> None:
        """Close the WebSocket connection without blocking on a dead peer."""
        ws = self.ws
        self.ws = None
        self.connected = False
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=1.0)
        except Exception:
            # A peer that already sent 1011 may never send a close frame.
            # Force the underlying transport closed instead of adding 10s.
            transport = getattr(ws, "transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass

    async def reconnect(self) -> None:
        """Reconnect to the gateway after a connection drop."""
        async with self._reconnect_lock:
            if self.connected and self.ws is not None:
                return
            LOG.info("reconnecting to gateway...")
            await self._cancel_heartbeat()
            await self._close_ws()
            await self.connect()

    async def close(self) -> None:
        """Close the gateway connection."""
        self.connected = False
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        self._reconnect_task = None
        await self._cancel_heartbeat()
        await self._close_ws()
        LOG.info("gateway connection closed")
