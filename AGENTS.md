# AGENTS.md — WA-call-claw Setup Guide for AI Agents

> **How to use this file:** This document is written as a **prompt/instruction** for AI
> agents (OpenClaw, Hermes, or any other agent). If you are an agent asked to
> **build / set up / deploy / troubleshoot** WA-call-claw, follow the steps below in
> order. Do not guess — read the referenced files, then execute.
>
> **Golden rule:** This repo is the SINGLE SOURCE OF TRUTH for code. The deployment
> directory (TMP) holds the runtime binary + config + logs. The active config in TMP
> is NEVER overwritten by deploy.

---

## 1. What is WA-call-claw?

An automated **WhatsApp voice call** system:

- **Bridge** (Go) — auto-answers incoming WhatsApp calls, forwards audio PCM over WebSocket, supports outgoing calls (dial → play WAV → hang up).
- **Voice Agent** (Python) — conversation pipeline: PCM → ElevenLabs STT → OpenClaw Gateway (LLM agent) → ElevenLabs TTS → PCM.
- **Supervisor** (bash) — keeps bridge + voice agent alive (auto-restart ≤15 seconds).
- **scripts/** — deploy, wacall CLI (outgoing call).

```
WhatsApp caller ──▶ Bridge (Go, :9090/ws) ──▶ Voice Agent (Python)
                       │                          │
                       │ audio PCM frames         ├─▶ ElevenLabs STT (Scribe v2)
                       │                          ├─▶ OpenClaw Gateway (agent chat.send)
                       │                          └─▶ ElevenLabs TTS (eleven_flash_v2_5)
                       │
                       └── smoke/calls/<call_id>/incoming-*.wav (WAV recordings)
```

> ✅ **CAPABILITY STATUS (verified 2026-08-15):** Inbound 1:1 calls are **two-way** —
> the bot hears the caller (recorded to WAV) AND the caller hears the bot's TTS
> playback (barge-in works). This is thanks to the multi-relay fix (PR #26,
> `connectAndAllocateAll` in engine_media.go) — do NOT "fix" it back to
> single-relay binding, inbound audio will break. Outgoing announcement calls are
> also verified. Group calls: in progress.

## 2. Prerequisites (check these first)

| Requirement | Minimum version | Check |
|---|---|---|
| Go | 1.25 | `go version` |
| Python | 3.12 | `python3 --version` |
| ffmpeg | any | `ffmpeg -version` (used by `scripts/wacall.py` for audio conversion AND `voice-agent/audio_convert.py` to decode MP3 greetings) |
| OpenClaw Gateway | running locally | `curl -s http://127.0.0.1:18789/health` or check `openclaw status` |
| systemd (user) | present | `systemctl --user status` (for the supervisor service) |

## 3. Repo structure

```
WA-call-claw/
├── AGENTS.md                 ← this file (setup instructions for agents)
├── README.md                 ← general documentation + architecture
├── LICENSE
├── config.example.json       ← example bridge config
├── bridge/                   ← Go code (module github.com/meowcaller-poc)
│   ├── cmd/meowcaller-poc/main.go
│   ├── internal/             ← whatsapp session, bridge protocol, relay, etc.
│   └── third_party/meowcaller/ ← meowcaller library (local fork)
├── voice-agent/              ← Python code (voice agent + gateway client)
│   ├── meowcaller_openclaw_voice.py  ← VOICE AGENT ENTRY POINT
│   ├── openclaw_gateway_client.py    ← WebSocket client to the gateway
│   ├── openclaw_session_resolver.py  ← resolves session key from sessions.json
│   ├── voice_context_router.py       ← routes context per caller
│   ├── run_openclaw_voice.sh         ← env wrapper + start
│   └── requirements.txt
├── scripts/
│   ├── deploy.sh             ← SYNC repo → TMP + build + restart
│   ├── wacall.py             ← outgoing call CLI
│   └── (no Telegram hooks — all outbound Telegram logging removed 2026-08-15)
└── supervisor/
    ├── supervisor.sh         ← Supervisor v3 (keeps 2 services alive)
    └── README.md
```

## 4. Build & Setup — STEP BY STEP

### Step 1: Build the bridge (Go)

```bash
cd bridge
go build -o meowcaller-poc ./cmd/meowcaller-poc
# binary appears at bridge/meowcaller-poc
```

Note: `go.mod` module = `github.com/meowcaller-poc`. The meowcaller library lives in
`third_party/meowcaller/` (local fork with the multi-relay PR #26 patch). Do not
switch to a remote dependency without coordination.

### Step 2: Set up the voice-agent venv

```bash
cd voice-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# requirements.txt: webrtcvad-wheels==2.0.14, pytest
```

Note: the voice agent has no heavy dependencies — STT/TTS go through the ElevenLabs
API, LLM through the OpenClaw gateway. No local models.

### Step 3: Set up the bridge config

Copy `config.example.json` → `config.yaml` (or `config.json`) into the deployment
directory, then fill it in for your environment. Example config:

```yaml
# config.yaml — bridge config
outgoing:
  allowlist: false        # ⚠️ MUST be a boolean, NOT [] — with [] the bridge fails to start
  # allowlist: ["+62812xxxxxxx"]  # allowlist mode (true = only these numbers)
bridge:
  listen: "127.0.0.1:9090"
  path: "/ws"
```

Config files are NOT committed (they are in .gitignore). The ACTIVE config lives in
the deployment directory (TMP), not in the repo.

### Step 4: Set up voice agent env vars

`run_openclaw_voice.sh` auto-loads most of the environment. These are required:

| Env var | Source | Notes |
|---|---|---|
| `ELEVENLABS_API_KEY` | ElevenLabs account | STT + TTS. If empty, the script looks for a local key file |
| `OPENCLAW_GATEWAY_TOKEN` | `openclaw.json` → `gateway.auth.token` | 48 characters. Auto-loaded from config if empty |
| `MIMO_API_KEY` | Xiaomi Mimo | used by llm_client.py (only for direct converse mode) |

Optional: `OPENCLAW_GATEWAY_URL` (default `ws://127.0.0.1:18789`),
`ELEVENLABS_TTS_VOICE_ID` (default `gmnazjXOFoOcWA59sd5m`),
`ELEVENLABS_TTS_SPEED` (default `0.94`).

> ### ⚠️ ELEVENLABS_API_KEY IS MANDATORY — NO EXCEPTIONS
>
> The voice agent **cannot work at all** without a valid `ELEVENLABS_API_KEY`:
> STT (Scribe) and TTS both go through the ElevenLabs API. There is no fallback
> and no local model.
>
> **Do NOT report "green light, ready for testing" until ALL of these are true:**
> 1. `ELEVENLABS_API_KEY` is set in the runtime environment (or the key file the
>    script auto-loads), AND
> 2. The key is **valid** (not a placeholder like `your-key-here`), AND
> 3. A live STT test passed (a real recording transcribed), AND
> 4. A live TTS test passed (a real MP3/WAV synthesized).
>
> A bridge that starts up and answers calls is NOT "ready" — without a working
> ElevenLabs key the bot answers the call and then silently fails every STT/TTS
> step. Check the voice-agent log for `elevenlabs` errors before claiming readiness.

### Step 4b: Opening greeting (set this BEFORE first test call)

The agent's first message on an answered call comes from `default_greeting` in the
bridge config. It supports three formats (paths relative to the config file's
directory):

| Format | Behavior |
|---|---|
| plain text, e.g. `"Hello, how can I help you?"` | synthesized with TTS at call time |
| `.txt` file path | file text is read, then synthesized with TTS |
| `.wav` file path | played **directly as-is, no TTS** (recommended: zero latency, no key needed for playback) |

**Defaults:** the repo ships a ready-to-use WAV greeting at
`voice-agent/assets/opening-ada-yang-bisa-kubantu.wav` (Indonesian for
"Halo, ada yang bisa kubantu?" / "Hello, how can I help you?").
If you don't have a custom greeting yet, point `default_greeting` at that WAV — the
bot will still speak it even while you sort out your own ElevenLabs voice.

**Do not skip this config.** Without `default_greeting` the call has an awkward dead
silence until the caller speaks. Set it during setup, before the first test call.

### Step 4c: Processing/waiting audio (optional but recommended)

While the gateway is thinking (after the caller finishes speaking, before the reply
starts), the caller would otherwise hear dead silence. Set `processing_audio` in the
config to a `.wav` file and the agent plays it right after each STT commit, before
the `chat.send` round trip:

| Config value | Behavior |
|---|---|
| `"processing_audio": "assets/processing-oke-tunggu-sebentar.wav"` | plays the WAV while waiting for the reply |
| omitted / `null` / empty string | feature disabled (no-op, silence while thinking) |

**Defaults:** the repo ships `voice-agent/assets/processing-oke-tunggu-sebentar.wav`
(Indonesian for "Oke, tunggu sebentar yah!" / "OK, wait a moment!" — 1.7 s, 16 kHz
mono PCM, same voice as the bot). Paths are relative to the config file's directory,
same as `default_greeting`.

**How it works (implementation notes):**
- Played in `_process_voice_turn`, after the gateway reconnect check and before
  `chat.send` — inside `_playback_lock`, so it can never overlap the reply playback.
- STT finals that arrive while it is playing are **ignored** (they would almost
  always be the agent's own audio picked up ambiently) — same reasoning as the
  opening greeting grace window. Log line: `processing audio played: call=... pcm_bytes=...`.
- The WAV must be 16-bit PCM mono (any rate; the agent resamples to 16 kHz).

### Step 4d: Incoming-call allowlist (recommended for private bots)

By default the bridge answers **every** incoming call. To restrict who can actually
reach the bot, set `incoming.allowlist` in the config:

| Config value | Behavior |
|---|---|
| `"incoming": { "allowlist": false }` (default) | answer every incoming call (legacy behavior) |
| `"incoming": { "allowlist": true, "allowlist_numbers": [...] }` | **only** listed callers are answered; everyone else is left ringing (never answered, never rejected) |

`allowlist_numbers` entries may be full JIDs (`"66984377057451@lid"`,
`"6281234567890@s.whatsapp.net"`) or phone numbers with/without `+`
(`"+6281234567890"`, `"6281234567890"`). An **empty list with `allowlist: true`
ignores every caller** (strict mode) — don't lock yourself out; include at least
your own number/LID.

**Runtime behavior:** disallowed calls are logged
(`incoming call IGNORED (not in allowlist): id=... peer=... phone=...`) and a
metadata entry is written with `status: failed`, reason
`ignored: caller not in allowlist`. The bridge never answers, records, or
bridges audio for them — the call is left ringing so **other devices (e.g. the
owner's phone) can still pick it up** (rejecting would kill the call everywhere).
The check runs before WAV recorder / sink setup.

### Step 5: Start the supervisor

```bash
cd supervisor
./supervisor.sh            # start (usually via systemd)
./supervisor.sh status     # check status without starting
```

Supervisor v3 keeps **2 services** alive: the Go bridge + the Python voice agent. If
either dies → auto-restart within ≤15 seconds (CHECK_INTERVAL=15, STARTUP_GRACE=8).

Optional systemd user service (auto-start on boot):

```bash
# ~/.config/systemd/user/meowcaller-supervisor.service
[Unit]
Description=MEOWcaller Supervisor
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=5
KillMode=process
ExecStart=/path/to/supervisor/supervisor.sh

[Install]
WantedBy=default.target
```

Then: `systemctl --user enable --now meowcaller-supervisor` (requires `loginctl enable-linger`).

## 5. Deploy (update code to runtime)

```bash
./scripts/deploy.sh          # rsync repo → TMP + go build + restart bridge + verify
./scripts/deploy.sh --dry    # preview files that would sync (no execution)
```

Deploy mapping:
- `bridge/` → `TMP/meowcaller-poc` (bridge runtime)
- `supervisor/` → `TMP/meowcaller-poc` (supervisor)
- `scripts/` → `TMP/meowcaller-poc/scripts`
- `voice-agent/` → `TMP/meowcaller-agent` (voice agent runtime)

⚠️ rsync WITHOUT `--delete` — runtime files (config.yaml, logs, .pid) in TMP stay safe.

## 6. Acceptance Test Cases (run ALL before reporting "ready")

**Rule:** do NOT report "green light / ready for testing" until **every** test case
below passes. A bridge that starts is NOT ready — a bot that answers calls but is
silently deaf/mute (bad ElevenLabs key) is NOT ready.

### 6.1 Prerequisites & build

| # | Test | Command | Expected |
|---|------|---------|----------|
| TC-01 | Go toolchain | `go version` | `go1.2x` reported, no error |
| TC-02 | Python 3 | `python3 --version` | `Python 3.1x` reported |
| TC-03 | Bridge binary builds | `cd bridge && go build -o meowcaller-poc ./cmd/meowcaller-poc` | exit 0; `bridge/meowcaller-poc` exists |
| TC-04 | Voice-agent venv | `cd voice-agent && .venv/bin/pip install -r requirements.txt` | exit 0; `webrtcvad` importable |
| TC-05 | Config file valid JSON | `python3 -c "import json;json.load(open('<deploy>/config.json'))"` | exit 0, no parse error |
| TC-06 | `outgoing.allowlist` is a **boolean** | inspect config | `true`/`false`, NOT `[]` (empty list crashes the bridge at startup) |

### 6.2 ElevenLabs (MANDATORY — no key, no voice)

| # | Test | Command | Expected |
|---|------|---------|----------|
| TC-07 | Key is set | `test -n "$ELEVENLABS_API_KEY" && echo set` (or the script's key file) | prints `set` |
| TC-08 | Key is NOT a placeholder | `echo "$ELEVENLABS_API_KEY" \| grep -c "your-key-here"` | prints `0` |
| TC-09 | **Live STT test** | run `voice-agent/run_stt_agent.sh <sample.wav>` (any real speech file) | transcript text printed, no HTTP 401/403 in log |
| TC-10 | **Live TTS test** | `ELEVENLABS_API_KEY=$KEY .venv/bin/python -c "from tts_client import ElevenLabsTTSClient; c=ElevenLabsTTSClient(...); d=c.synthesize('hello'); assert len(d)>0; open('/tmp/tts-test.mp3','wb').write(d)"` | file written > 0 bytes, no 401/403 |

> If TC-09 or TC-10 fails with 401/403: the key is invalid/expired — STOP, get a
> valid key. Do not proceed.

### 6.3 Greeting

| # | Test | Command | Expected |
|---|------|---------|----------|
| TC-11 | `default_greeting` is set | inspect bridge config | non-empty value |
| TC-12 | Greeting file/text resolves | if a path: `test -f <resolved-path>` | file exists (relative paths resolve against the config file's directory) |
| TC-12b | `processing_audio` resolves (optional feature) | if set: `test -f <resolved-path>` | file exists; when unset, feature is disabled (no-op) |
| TC-12c | `incoming.allowlist` is a **boolean** | inspect config | `true`/`false`, NOT `[]` (same pitfall as `outgoing.allowlist`) |
| TC-12d | `incoming.allowlist_numbers` non-empty when allowlist=true | inspect config | ≥1 entry (empty list + true = ignores EVERY caller) |

> No custom greeting yet? Point `default_greeting` at the shipped
> `voice-agent/assets/opening-ada-yang-bisa-kubantu.wav` — the bot will speak it
> without needing TTS.

### 6.4 Runtime health

| # | Test | Command | Expected |
|---|------|---------|----------|
| TC-13 | Supervisor up | `cd supervisor && ./supervisor.sh status` | both services alive (bridge + voice agent) |
| TC-14 | Bridge HTTP/WS listening | `ss -tlnp \| grep 9090` | `127.0.0.1:9090` LISTEN |
| TC-15 | Voice agent connected to bridge | `grep "bridge connected" <voice-agent log>` | at least one line |
| TC-16 | Voice agent connected to gateway | `grep "gateway connected" <voice-agent log> \| tail -1` | `connId=...` present |
| TC-17 | **Protocol negotiation OK** | `grep "protocol=" <voice-agent log> \| tail -1` | `protocol=v3` or `protocol=v4` — NOT `protocol mismatch` / close 1002 |
| TC-18 | Heartbeat healthy | `grep "heartbeat ok" <voice-agent log> \| tail -3` | RTT logged, no reconnect storm |

### 6.5 End-to-end call tests (real WhatsApp calls)

| # | Test | How | Expected |
|---|------|-----|----------|
| TC-19 | Inbound: auto-answer + record | call the number; then `ls smoke/calls/ \| tail -1` | a new `call_id` folder with `.wav` + `metadata.json` |
| TC-20 | Inbound WAV has audio | `ls -l <newest .wav>` | size > 10 KB (real frames, not silence) |
| TC-21 | **Two-way audio** | call and listen | caller hears the bot's greeting/TTS playback AND bot hears the caller |
| TC-22 | Barge-in (if `barge_in.enabled`) | interrupt the bot mid-speech | bot stops, listens, responds |
| TC-23 | Outbound announcement (if `outgoing.enabled`) | trigger `POST /api/call` with an allowlisted number | dial → WAV plays → hangup after `hangup_after_play_sec` |

> If TC-21 fails (caller can't hear the bot): the multi-relay fix is missing —
> `connectAndAllocateAll` in `engine_media.go` (PR #26). Single-relay binding
> breaks inbound audio. Do NOT "fix" it back.

### 6.6 Log hygiene (final sweep)

| # | Test | Command | Expected |
|---|------|---------|----------|
| TC-24 | No auth errors | `grep -iE "401\|403\|unauthorized\|invalid.*key" <voice-agent log>` | no output |
| TC-25 | No protocol mismatch | `grep -i "protocol mismatch" <voice-agent log>` | no output |
| TC-26 | No crash loops | `grep -c "Traceback" <voice-agent log>` | `0` (or only known, explained errors) |

**All TC-01..TC-26 passing = ready.** Anything less = still in progress — report the
failing TC numbers with the actual log lines, not "ready".

## 7. Daily Operations

| Action | Command |
|---|---|
| Check status | `./supervisor/supervisor.sh status` |
| Restart voice agent | `kill $(cat .meowcaller-voice.pid)` → supervisor auto-restarts ≤15s |
| Restart bridge | `kill $(cat .meowcaller-bridge.pid)` → supervisor auto-restarts ≤15s |
| Voice agent log | `tail -f <TMP>/meowcaller-agent/meowcaller-openclaw-voice.log` |
| Supervisor log | `tail -f <TMP>/meowcaller-poc/meowcaller-supervisor.log` |
| Gateway log | `/tmp/openclaw/openclaw-YYYY-MM-DD.log` (UTC; WIB = UTC+7) |
| Outgoing call | `python3 scripts/wacall.py call <phone> --audio <file.wav> [--delay 3]` |

## 8. Troubleshooting

> Full debugging guide: **`AGENT-DEBUGGING.md`** — log inventory, the "golden path"
> of a healthy call, per-symptom walkthroughs (no opening audio, no reply, cut-off
> replies, one-way audio, protocol mismatch, heartbeat loops), diagnostic one-liners,
> and reporting rules. Read it before touching anything.

### "gateway heartbeat failed" → reconnect loop
- Symptom: repeated `gateway heartbeat failed (), scheduling reconnect` lines.
- Check first: `ss -tnp | grep 18789` — look at Recv-Q on the voice agent side.
  Recv-Q growing (>10KB) = data arriving but not being read → receive path problem.
- Pong timeout = `max(heartbeat_interval * 3, 15)` seconds (NOT 5s — false positives).
- If the gateway itself is down: `openclaw gateway status` / restart the gateway.

### Error `sent 1011 (internal error) keepalive ping timeout`
- Cause: the websockets library's built-in ping (default 20s) conflicts.
- Fix: bridge/gateway connections MUST use `ping_interval=None, ping_timeout=None`
  (already applied in `connect_bridge()` and `connect_gateway()` — do not change back).

### Bridge fails to start
- Check `config.yaml` → `outgoing.allowlist` MUST be a boolean (`false`), not `[]`.
- Check the bridge startup log for the allowlist line.

### Voice agent crash-loop
- Check `meowcaller-openclaw-voice.log` for a traceback.
- Make sure `.venv` exists: `cd voice-agent && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

### Call comes in but no audio
- Walk the pipeline: STT commit? ("barge-in: STT confirmed") → Gateway response?
  → TTS decoded? Each stage has its own log line — find the stage that stops.

## 9. Agent Rules (IMPORTANT)

1. **Never commit secrets.** Tokens, API keys, passwords NEVER go into the repo.
   If you find one in a file you are about to commit: redact it into an env var first,
   then commit.
2. **Active config ≠ repo config.** Config files in the deployment TMP are never overwritten.
3. **Edit pattern:** do not edit 2 locations in one parallel block — race conditions
   (the first edit gets overwritten). Edit one at a time.
4. **Heartbeat:** ping every 5 seconds, pong timeout 15 seconds, RTT logged every 60 seconds.
   Do not change without coordination — result of investigation on 2026-08-15.
5. **This repo is the single source of truth.** All code changes must land here first,
   then `deploy.sh` syncs them to runtime.
