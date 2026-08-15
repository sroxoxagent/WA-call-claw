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
> also verified. Group calls: in progress. Details: `bridge/VERIFIED.md`.

## 2. Prerequisites (check these first)

| Requirement | Minimum version | Check |
|---|---|---|
| Go | 1.25 | `go version` |
| Python | 3.12 | `python3 --version` |
| ffmpeg | any | `ffmpeg -version` (used by wacall.py for audio conversion) |
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

## 6. Installation Verification

```bash
# 1. Bridge up?
curl -s http://127.0.0.1:9090/health || curl -s http://127.0.0.1:9090/

# 2. Voice agent connected to bridge + gateway?
tail -20 /path/to/meowcaller-agent/meowcaller-openclaw-voice.log
# Should show: "bridge connected", "gateway connected: connId=...",
#              "gateway heartbeat started (interval=5s)"

# 3. Heartbeat healthy (ping every 5s, RTT logged every 60s)?
grep "heartbeat ok" /path/to/meowcaller-agent/meowcaller-openclaw-voice.log | tail -3

# 4. Test an incoming call: call the WhatsApp number connected to the bridge.
#    Afterwards: ls smoke/calls/ → a call_id folder with WAV + metadata.json
#    Expected on inbound 1:1: caller audio recorded AND caller hears the bot's
#    TTS playback (two-way, verified 2026-08-15 22:05). If the caller can't hear
#    the bot, check the multi-relay fix is intact (connectAndAllocateAll in
#    engine_media.go, PR #26) — single-relay binding breaks inbound audio.
```

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
