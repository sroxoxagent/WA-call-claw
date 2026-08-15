# MEOWcaller Voice Agent

Full conversational voice agent for the MEOWcaller WebSocket audio bridge.

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  MEOWcaller      │     │  MEOWcaller      │     │  MEOWcaller      │
│  User (caller)   │────▶│  Bridge (WS)     │────▶│  Voice Agent     │
│                  │◀────│                  │◀────│                  │
└──────────────────┘     └──────────────────┘     └──────────────────┘
                                    │                       │
                                    │  binary PCM frames    │
                                    │  + JSON control msgs  │
                                    ▼                       ▼
                           ┌────────────────────────────────────────┐
                           │         Voice Pipeline                 │
                           │                                        │
                           │  PCM in ─▶ ElevenLabs STT (Scribe v2) │
                           │           ─▶ OpenClaw Gateway (agent)  │
                           │           ─▶ ElevenLabs TTS            │
                           │           ─▶ PCM out (s16le 16kHz)    │
                           └────────────────────────────────────────┘
```

## Flow (per call)

1. **call_started** received from bridge → agent opens ElevenLabs STT session
2. **Binary PCM frames** from bridge → forwarded to ElevenLabs STT WebSocket
3. **Committed/final transcript** from STT → sent to OpenClaw via `chat.send` RPC
4. **Agent response text** (streamed as deltas) → accumulated and synthesized via TTS
5. **TTS audio** → converted to PCM s16le mono 16 kHz → chunked into 60 ms frames
6. **Binary PCM frames** → sent back over bridge WebSocket
7. **audio_playing** / **audio_done** control messages sent to bridge around playback
8. **call_ended** / **cancel** → cleanup STT session, reset state

The gateway `chat.send` RPC is called with a **resolved session key**:

1. `openclaw_session_resolver.py` scans the gateway's `sessions.json`
2. It indexes every `agent:main:<channel>:direct:<id>` session (channel priority:
   webchat > whatsapp > telegram > unknown, then most recently active)
3. The caller's phone (from bridge `remote_phone` / JID→phone mapping) selects the
   matching session — so the gateway loads that session's existing context/memory
4. Fallback when no match: `agent:main:voice:<call_id>` (isolated)

This keeps conversation context persistent across calls instead of starting fresh.

## Voice Context Routing

The agent loads `voice_context_config.json` by default. It explicitly allowlists:

- `IDENTITY.md`
- `USER.md`
- `SOUL.md`
- `MEMORY.md`
- one caller-specific `chat-whatsapp-direct-+<phone>.memory.md`

The caller JID/LID comes from the `call_started` event. JID/LID mapping is resolved by `voice_context_router.py`; a LID is never guessed as a phone number. The selected context is embedded in the user message sent to OpenClaw. Unknown callers receive restricted context and never receive the owner MD profile.

Historical chat transcripts are never loaded.

## Configuration

All secrets are loaded from environment variables or existing workspace config.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ELEVENLABS_API_KEY` | *(auto-loaded)* | ElevenLabs API key (loaded from existing STT wrapper) |
| `OPENCLAW_GATEWAY_TOKEN` | *(auto-loaded)* | OpenClaw gateway token (loaded from openclaw.json) |
| `OPENCLAW_GATEWAY_URL` | `ws://127.0.0.1:18789` | OpenClaw gateway WebSocket URL |
| `OPENCLAW_GATEWAY_TIMEOUT` | `60` | Gateway request timeout in seconds |
| `MEOWCALLER_BRIDGE_URL` | `ws://127.0.0.1:9090/ws` | MEOWcaller bridge WebSocket URL |
| `MEOWCALLER_AGENT_ID` | `openclaw-voice-agent` | Agent identifier |
| `ELEVENLABS_STT_MODEL` | `scribe_v2_realtime` | STT model |
| `ELEVENLABS_STT_LANGUAGE` | `id` | STT language code |
| `ELEVENLABS_TTS_VOICE_ID` | `gmnazjXOFoOcWA59sd5m` (Kira) | TTS voice |
| `MEOWCALLER_AGENT_LOG_LEVEL` | `INFO` | Log level |

### Agent Config File (`config.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `vad.provider` | `webrtcvad` | VAD engine: `webrtcvad` (client-side commit, original behavior) or `elevenlabs` (server-side commit via `commit_strategy=vad`, client VAD kept as safety net). |
| `vad.silence_ms` | `1500` | Silence threshold for **client-side** commit (webrtcvad provider, and fallback for elevenlabs provider). |
| `vad.min_silence_duration_ms` | `500` | Server VAD: silence that ends a turn (elevenlabs provider only). |
| `vad.min_speech_duration_ms` | `200` | Server VAD: minimum speech before a turn can commit (elevenlabs provider only). |
| `vad.vad_silence_threshold_secs` | `null` | Server VAD: speech-vs-silence sensitivity in seconds (elevenlabs provider only). |
| `stt.model` | `scribe_v2_realtime` | STT model override. |
| `tts.voice_id` / `tts.speed` | Kira / `0.94` | TTS voice and speed. |
| `barge_in.enabled` | `false` | Allow the caller to interrupt TTS playback. |
| `session.load_memory` | `true` | Load caller's `.memory.md` into the voice prompt. |

Switch VAD provider by editing `config.json` only — no code changes needed:

```json
"vad": { "provider": "elevenlabs", "silence_ms": 1500, "min_silence_duration_ms": 500 }
```

### Key Sources

- **ElevenLabs key**: `CUSTOM_SKILL/elevenlabs-stt/elevenlabs_stt.sh` (the `KEY=` line)
- **Gateway token**: `/opt/openclaw/openclaw.json` → `gateway.auth.token`
- **Device identity**: `/opt/openclaw/identity/device.json` (for gateway auth handshake)

## Files

| File | Purpose |
|------|---------|
| `meowcaller_openclaw_voice.py` | Main agent — orchestrates STT → Gateway → TTS pipeline |
| `meowcaller_converse_agent.py` | Direct-Mimo fallback agent (preserved for reference) |
| `openclaw_gateway_client.py` | OpenClaw gateway WebSocket client with heartbeat |
| `openclaw_session_resolver.py` | Scans sessions.json → resolves caller phone to active session key |
| `voice_context_router.py` | Identity resolution and session memory routing |
| `md_profile_loader.py` | Config-driven MD context file loader |
| `protocol.py` | Bridge WebSocket protocol message parsing |
| `tts_client.py` | ElevenLabs TTS REST streaming client |
| `audio_convert.py` | WAV→PCM conversion, resampling, chunking |
| *(removed 2026-08-15)* | No Telegram logging — debug hook + event watcher deleted |
| `run_openclaw_voice.sh` | Launcher — auto-loads keys, starts agent |
| `voice_context_config.json` | Identity mappings + MD profile config |
| `tests/` | Test suite (no real API calls) |

## Run

```bash
# Voice agent (OpenClaw gateway path)
./run_openclaw_voice.sh --bridge-url ws://127.0.0.1:9090/ws
```

Logs are written to `meowcaller-openclaw-voice.log`.

## Tests

```bash
cd /opt/wa-call-claw/voice-agent
python3 -m pytest tests/ -v
```

Tests use mocks for gateway, LLM, TTS, and WebSocket — no real API calls are made.

## Known Limitations

1. **Single-call only**: The agent handles one call at a time. A second incoming call will cancel the first.
2. **No TTS cancellation mid-chunk**: Cancel is checked between audio chunks, not within them. A ~100 ms tail may play after cancel.
3. **chat.send delta accumulation**: The gateway streams response text as deltas. If the gateway times out before sending all deltas, the response may be truncated.
4. **PCM format**: TTS output is resampled to 16 kHz s16le mono for bridge compatibility.
5. **No echo cancellation**: The agent does not perform acoustic echo cancellation.
6. **STT → Gateway → TTS latency**: There is a gap between the user finishing speech and the agent responding. This is inherent to the pipeline.
