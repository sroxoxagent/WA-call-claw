# MEOWcaller POC

Minimal native WhatsApp call auto-answer and peer audio WAV recorder with
WebSocket audio bridge, plus one-way outbound announcement calls.

## Scope (Locked)

- **Incoming calls** — auto-answered, recorded as WAV, forwarded to agent
- **Outgoing calls** — dial → play WAV announcement → auto-hangup (see [Outgoing Calls](#outgoing-calls))
- **Auto-answer** — all incoming calls are answered automatically
- **WAV recording** — peer audio saved as WAV
- **WebSocket bridge** — bidirectional audio forwarding to external agent
- **No Docker** — runs natively on the server
- **MP3/audio auto-convert (CLI)** — `wacall.py` converts MP3/OGG/M4A → 16-bit PCM WAV via ffmpeg before dialing; the bridge itself still requires 16-bit PCM WAV
- **No API/webhook** — metadata stored as JSON files

## Architecture

```
WhatsApp incoming call
        │
        ▼
MEOWcaller auto-answer
        │
        ├──── Agent connected? ──── YES ──→ TeeSink(WAV + Agent) + AgentSource
        │                                  │
        │                                  ├─ Caller audio → Agent (binary WebSocket)
        │                                  └─ Agent audio → Caller (via Player)
        │
        └──── NO ──→ WAVRecorder + Fallback playback (announcement)
                │
                ▼
        Save metadata.json + incoming.wav
```

## Build

```bash
cd meowcaller-poc
go mod tidy
go build -o meowcaller-poc ./cmd/meowcaller-poc
```

## Run

```bash
# Default config (no file needed):
./meowcaller-poc

# With custom config:
./meowcaller-poc -config config/config.yaml
```

On first run, a QR code will be shown for WhatsApp pairing. After pairing, the service
will auto-reconnect on subsequent runs using the stored session.

## WebSocket Audio Bridge

When enabled, the bridge server accepts one WebSocket agent connection at a time.
When a call is answered with an agent connected, audio flows bidirectionally.

### Configuration

```yaml
bridge:
  enabled: true        # Set to true to enable the bridge
  listen: "127.0.0.1:9090"  # Bind address (localhost only for security)
  path: "/ws"          # WebSocket URL path
```

### Connecting an Agent

```bash
# Using websocat:
websocat ws://127.0.0.1:9090/ws

# Using Python (websockets):
import asyncio, websockets
async with websockets.connect("ws://127.0.0.1:9090/ws") as ws:
    ...
```

### Protocol

#### Binary Messages (Audio)

Each binary WebSocket message is a raw **s16le mono 16 kHz PCM frame**:
- **Size**: 1920 bytes (960 samples × 2 bytes/sample)
- **Cadence**: 60 ms per frame (matches meowcaller's codec)
- **Sample format**: signed 16-bit little-endian, range [-32768, 32767]

**Direction: MEOWcaller → Agent**: Caller's decoded audio (forwarded from `AudioSink.WriteFrame`)

**Direction: Agent → MEOWcaller**: Agent's audio to replay to caller (read by `AudioSource.ReadFrame`)

#### JSON Messages

**Call metadata** (MEOWcaller → Agent, sent once when call is answered):

```json
{
  "type": "call_started",
  "call_id": "20260813T185500Z-abc123",
  "caller_id": "120363xxx@g.us",
  "started_at": "2026-08-13T18:55:00+07:00",
  "audio": {
    "sample_rate": 16000,
    "channels": 1,
    "encoding": "s16le",
    "frame_size": 1920,
    "frame_ms": 60
  }
}
```

**Hangup command** (Agent → MEOWcaller, optional):

```json
{
  "type": "hangup",
  "reason": "agent done"
}
```

### Behavior

- **Agent connected + incoming call**: Audio is bridged bidirectionally. Caller's audio is forwarded to the agent as binary frames. Agent's binary frames are decoded and played into the call. WAV recording continues in parallel via `TeeSink`.
- **No agent connected**: Existing fallback behavior — WAV recording + optional announcement playback + hangup-after-play.
- **Agent disconnects mid-call**: The call hangs up automatically.
- **Agent sends hangup**: The call ends.
- **Max duration**: The `max_call_duration` safety net still applies.

### Example Agent Script (Python)

```python
import asyncio
import struct
import websockets
import json

async def handle_call():
    async with websockets.connect("ws://127.0.0.1:9090/ws") as ws:
        # Wait for call metadata
        msg = await ws.recv()
        meta = json.loads(msg)
        print(f"Call started: {meta['call_id']} from {meta['caller_id']}")

        # Read caller audio frames and process them
        while True:
            try:
                frame = await ws.recv()
                if isinstance(frame, bytes):
                    # frame is s16le PCM, 960 samples
                    samples = struct.unpack(f"<{len(frame)//2}h", frame)
                    # Process audio...
            except websockets.exceptions.ConnectionClosed:
                break

        # Optionally hang up
        await ws.send(json.dumps({"type": "hangup", "reason": "done"}))

asyncio.run(handle_call())
```

## Outgoing Calls

One-way outbound calls: dial a number → wait for the peer to answer →
silence (`default_delay_ms`) → play a WAV announcement → hang up
`hangup_after_play_sec` seconds after the WAV finishes (total call time =
delay + WAV duration + hangup grace). If the peer never answers, the call
hangs up after `ring_timeout`.

### Enabling

```yaml
bridge:
  enabled: true
outgoing:
  enabled: true
  allowlist: true                # true = only numbers found in session_store_path may be called; false = any number can be called
  session_store_path: "/path/to/sessions.json"   # source of truth for the allowlist
  session_allowlist_ttl: "60s"      # how often the dynamic list is refreshed
  max_calls_per_hour: 10
  default_delay_ms: 1000            # silence before playback starts
  hangup_after_play_sec: 3          # grace period after WAV ends
  ring_timeout: "60s"
  audio_dir: "/path/to/audio"       # directory where `audio` names are resolved
```

`allowlist` (default `true`): when `true`, only numbers found in the OpenClaw
sessions store (`session_store_path`, i.e. numbers that have a direct chat
session) can be called; when `false`, the allowlist check is skipped entirely
and any valid E.164 number can be called.
Keep it `true` unless you really need open dialing.

### CLI

```bash
scripts/wacall.py call 6281234567890 --audio pesan.wav          # delay = config default
scripts/wacall.py call 6281234567890 --audio pesan.mp3          # MP3 auto-converted via ffmpeg
scripts/wacall.py call 6281234567890 --audio pesan.wav --delay 3
scripts/wacall.py --bridge http://127.0.0.1:9090 call ...        # custom bridge URL
```

The CLI POSTs to `POST /api/call` on the bridge HTTP server (same port as the
WebSocket). The agent WebSocket connection is never used for outbound
requests.

### Audio format

- **Bridge requirement**: 16-bit PCM RIFF/WAVE only. Anything else is
  rejected with `not a 16-bit PCM RIFF/WAVE file`.
- **CLI convenience**: `wacall.py` detects non-WAV inputs (MP3, OGG, M4A, ...)
  by magic bytes and converts them to 48 kHz mono s16le WAV via ffmpeg before
  dialing. The converted file is written to `$TMP` (or `/tmp`) and printed in
  the acceptance output (`audio: ... (converted from MP3 via ffmpeg)`).
- **Sample rates**: both 16 kHz (original test asset) and 48 kHz (ffmpeg
  default above) are verified working end-to-end.

### Safeguards

| Guard | Behavior |
|-------|----------|
| Allowlist toggle | `allowlist: true` (default) — only numbers found in `session_store_path` (OpenClaw sessions.json, i.e. direct chat sessions) may be called; `false` — allowlist check skipped, any valid E.164 number can be called. |
| Allowlist source | Single source of truth: `session_store_path`. Numbers are extracted from session keys/chat IDs (62-prefix pattern), refreshed every `session_allowlist_ttl`. No static number list in config. |
| Rate limit | At most `max_calls_per_hour` calls per rolling hour (sliding window). |
| Format | Phone must be E.164 without leading `+` (7-15 digits). |
| Ring timeout | Unanswered calls hang up after `ring_timeout` (default 60s). |

Rejections are logged by the bridge and returned to the caller as HTTP 409
with a `reason`. Completed calls write a `call-ended-*.json` event to the
spool with `direction: "outgoing"`, `target_phone` and `outcome`
(`completed` / `no-answer` / `busy` / `failed`).

### API

```
POST /api/call
{"type":"outgoing_call","phone":"6281234567890","audio":"pesan.wav","delay_ms":1000}

200 → {"type":"outgoing_call_ack","status":"accepted","call_id":"..."}
409 → {"type":"outgoing_call_ack","status":"rejected","reason":"..."}
503 → outgoing disabled
```

`audio` is resolved relative to `outgoing.audio_dir` (fallback: playback
file). `delay_ms` overrides `default_delay_ms` when present.

## Output Structure

```
/var/lib/meowcaller/calls/
├── 20260813T185500Z-abc123/
│   ├── incoming.wav
│   └── metadata.json
├── 20260813T190100Z-def456/
│   ├── incoming.wav
│   └── metadata.json
```

### metadata.json

```json
{
  "call_id": "20260813T185500Z-abc123",
  "direction": "incoming",
  "remote_jid": "628xxxxxxxx@s.whatsapp.net",
  "started_at": "2026-08-13T18:55:00+07:00",
  "ended_at": "2026-08-13T18:57:12+07:00",
  "audio_file": "incoming.wav",
  "format": "wav",
  "status": "completed",
  "duration_ms": 132000
}
```

## Dependencies

- [purpshell/meowcaller](https://github.com/purpshell/meowcaller) — WhatsApp VoIP Go library
- [go.mau.fi/whatsmeow](https://github.com/tulir/whatsmeow) — WhatsApp Web multi-device API
- [coder/websocket](https://github.com/coder/websocket) — WebSocket library for Go

## Limitations

- **Peer audio only**: The WAV file contains only the remote caller's voice. Agent audio is played via Player but not recorded separately.
- **Bridge accepts WAV only**: Server-side playback requires 16-bit PCM WAV. Non-WAV files must be converted first — the CLI does this automatically (see [Audio format](#audio-format)), but direct `POST /api/call` callers must send a WAV path.
- **Outgoing = fixed announcement**: Outbound calls play a pre-rendered WAV only — no live agent audio on outbound calls yet (Phase 5).
- **Single concurrent call**: The POC handles one call at a time by design.
- **Single agent connection**: Only one WebSocket agent can be connected at a time. Second connection gets HTTP 409.
- **No auth on WebSocket**: POC accepts all connections. Production should add token/auth.
- **No audio resampling**: Agent must send 16 kHz mono s16le frames matching meowcaller's format.

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Auto-answer + WAV recording + metadata + WebSocket bridge | This POC |
| 2 | WAV→MP3 conversion, retention policy, logging | Partial (CLI auto-convert done; server-side retention deferred) |
| 3 | Two-way audio mixing, multi-agent | Deferred |
| 4 | Outgoing calls (WAV announcement, allowlist, rate limit) | Done — verified 2026-08-15 (see VERIFIED.md) |
| 5 | Outgoing live-agent audio, webhooks, database, dashboard | Deferred |
