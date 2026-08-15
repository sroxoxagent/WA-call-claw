# AGENT-DEBUGGING.md — How to debug WA-call-claw

**Read this BEFORE touching anything.** Every symptom maps to a log line. Find the
log line first, then fix the cause. Never guess.

---

## 1. Log inventory (what produces what)

| Log | Written by | Format | Tells you |
|---|---|---|---|
| `<voice-agent>/meowcaller-openclaw-voice.log` | Python voice agent | `2026-08-15 22:05:41,412 INFO meowcaller.<module>: msg` | The **full call lifecycle**: call start, greeting, STT, gateway round-trip, TTS, playback, barge-in |
| `<bridge>/restart-bridge-YYYYMMDD-HHMM.log` | Go bridge | `[meowcaller] 2026/08/15 22:29:45 main.go:72: msg` | WhatsApp connection, WS bridge, agent connect, call events at the protocol level |
| `<bridge>/meowcaller-supervisor.log` | supervisor.sh | `[2026-08-15 21:21:36] ✅ Bridge up` | Start/restart/health of the 2 services (bridge + voice agent) |
| `<voice-agent>/meowcaller-stt-agent.log` | STT-only agent (optional mode) | same as voice agent | Only used if you run the STT agent separately |
| `smoke/calls/<call_id>/metadata.json` | bridge, per call | JSON | Call facts: direction, JID, start/end, duration, **frame_count** (audio health) |
| `smoke/calls/<call_id>/incoming-*.wav` | bridge, per call | WAV 16-bit PCM | The raw caller audio recording |
| `smoke/events/` | bridge | JSON event files | Raw protocol events (debug depth) |

> `<DEPLOY_DIR>` = your deployment directory (where config + binaries live). Paths
> differ per machine — find yours with `find / -name "meowcaller-openclaw-voice.log" 2>/dev/null`.

## 2. How to read the logs

### 2.1 Anatomy of a line

```
2026-08-15 22:05:41,412 INFO meowcaller-openclaw-voice: call started: 20260815T150541Z-51e434e1 from 66984377057451@lid
│              │         │                       │
│              │         │                       └─ message (the actual event)
│              │         └─ module (which component logged it)
│              └─ level: INFO (normal) / WARNING (degraded) / ERROR (broken)
└─ local timestamp — ALWAYS correlate with metadata.json started_at/ended_at
```

Module names: `meowcaller-openclaw-voice` (orchestrator), `meowcaller-converse`
(STT), `meowcaller.gateway` (OpenClaw gateway client), `meowcaller.session_resolver`,
`meowcaller.voice_context_router`, `meowcaller.md_profile_loader`, `httpx` (HTTP calls).

### 2.2 The golden path — one healthy call looks like this

Real example (verified two-way call, 2026-08-15 22:05). Read top to bottom:

| Phase | Log line (abridged) | Meaning |
|---|---|---|
| 1. Call accepted | `call started: <call_id> from <jid>` | inbound call detected |
| 2. Caller identified | `caller phone resolved by WhatsApp: +628...` | LID → phone mapping OK |
| 3. Context loaded | `session resolved (exact): phone=+62****03065 → key=agent:main:direct:...` | right Gateway session found |
| 4. STT up | `ElevenLabs STT WebSocket connected` | STT session live |
| 5. Greeting | `greeting loaded from config wav (49040 bytes PCM)` → `opening audio played: ... pcm_bytes=49040` | **opening played to caller** |
| 6. Caller speaks | `STT segment committed after 1280ms silence` → `final transcript: Jelasin tentang bridge in toh...` | caller audio heard + transcribed |
| 6b. Waiting cue | `processing audio played: call=... pcm_bytes=...` | "Oke, tunggu sebentar yah!" played while gateway thinks (config `processing_audio`; absent = disabled) |
| 7. Think | `chat.send ack: runId=... status=started` | turn sent to OpenClaw |
| 8. Answer ready | `gateway response: Barge in itu fitur yang bikin kamu bisa motong aku...` | model replied |
| 9. Speak | `HTTP Request: POST https://api.elevenlabs.io/v1/text-to-speech/... "HTTP/1.1 200 OK"` → `TTS decoded (container): pcm_bytes=335204` | TTS synthesized |
| 10. Playback | `barge-in armed by VAD` | reply playing to caller (armed = listening for interruptions) |

If you see phases 1→10 in order, the call worked. **Find the FIRST missing phase —
that's your bug.**

## 3. Debug cases

### Case A — No opening audio (caller hears silence)

Walk the chain, stop at the first missing line:

1. `grep "call started" <voice-agent log> | tail -1` — was the call even accepted?
   - Missing → bridge/WhatsApp problem, see Case F.
2. `grep "greeting loaded" <voice-agent log> | tail -1`
   - `greeting loaded from config wav (49040 bytes PCM)` → config OK.
   - `greeting loaded from wav file: <path>` → path from config resolved.
   - `greeting loaded from mp3 file` / decode path → **ffmpeg required** to decode
     MP3 greetings (`voice-agent/audio_convert.py`). If it fails, run
     `ffmpeg -version` — missing ffmpeg = MP3 greetings silently fail. Note the
     binary may live in `~/.local/bin` (not on default PATH); `run_openclaw_voice.sh`
     adds it, but if you launch the agent manually, export PATH first.
   - Nothing → `default_greeting` not set in bridge config, or path wrong (relative
     paths resolve against the **config file's directory**).
3. `grep "opening audio played" <voice-agent log> | tail -1`
   - Present → the agent **sent** the audio; if the caller still hears nothing the
     problem is below the agent: the **multi-relay fix** (see Case D).
   - Missing → audio pipeline failed; check `grep -i "error\|traceback" <voice-agent log> | tail`.
4. Sanity: is the WAV valid? `file voice-agent/assets/opening-ada-yang-bisa-kubantu.wav`
   → `WAVE audio, 16-bit PCM` (use the shipped WAV as a known-good fallback).

### Case B — No reply after the caller speaks

1. **Did STT hear the caller?**
   - `grep "final transcript" <voice-agent log> | tail -1` — if missing, the caller's
     audio never became text. Check `grep -i "stt\|elevenlabs" <voice-agent log> | tail -10`:
     - `401/403` → **invalid/expired ElevenLabs key** (the #1 cause of "bot is deaf").
     - `STT WebSocket connected` never appears → network/firewall to api.elevenlabs.io.
   - If the caller is silent on the recording too (WAV tiny, `frame_count` low) → the
     **inbound audio path** is broken (Case D), not STT.
2. **Did the gateway round-trip happen?**
   - `grep "processing audio played" <voice-agent log> | tail -1` — present = the
     waiting cue fired after STT commit (feature `processing_audio` works).
     **Missing but configured** → check `grep "processing audio" <voice-agent log>`:
     - `processing audio loaded from wav file: ...` at startup = config OK; if the
       play line is still missing the WAV may be empty/undecodable
       (`processing audio decoded to empty PCM`).
     - `processing_audio file not found: ...` = bad path (relative paths resolve
       against the config file's directory).
   - `grep "chat.send ack" <voice-agent log> | tail -1` — missing/timeout →
     gateway unreachable. Check `grep -i "gateway" <voice-agent log> | tail -10`:
     - `gateway heartbeat failed ()` → connection is flapping (see Case F).
     - `chat.send rejected: ...` → session key invalid / dmPolicy block (check
       `sessions.json` for the exact key format, e.g. `agent:main:direct:+628...`).
   - `grep "gateway response" <voice-agent log> | tail -1` — missing → the model
     didn't answer in time (gateway busy / long queue). Look for `WARNING` timeouts.
3. **Did TTS produce audio?**
   - `grep "TTS decoded" <voice-agent log> | tail -1` — missing → check the `httpx`
     lines: `200 OK` = fine; `401/403` = key problem; `429` = rate limit (slow down,
     check TTS quota); `5xx` = ElevenLabs outage.
4. **Was it played?** `grep "barge-in armed\|playback" <voice-agent log> | tail -3`.

### Case C — Reply cut off mid-sentence

1. `grep -E "playback stopped|skip TTS|call session changed" <voice-agent log> | tail -5`
   - `playback stopped by barge-in` → the **caller interrupted** (works as designed)
     OR VAD false-triggered on the bot's own audio. Check what STT heard right before:
     `grep "final transcript\|transcript_partial" <voice-agent log> | tail -5` — if it
     transcribed the bot's own words, raise the VAD silence/speech thresholds or tune
     `barge_in` config.
   - `call session changed, skip TTS` → the call state changed mid-turn (call dropped
     or new call arrived); check `grep "call started\|call ended" <voice-agent log> | tail -5`.
2. **Cut at the same spot every time?** → TTS stream truncated. Check the `httpx`
   line for that turn (`200 OK` but short `pcm_bytes`) — retry/network. Also verify
   `ELEVENLABS_TTS_SPEED` (0.94 default; extreme speeds can clip).
3. **Gateway answer itself was short?** `grep "gateway response" <voice-agent log> | tail -1`
   — the model may have stopped early (timeout). The voice prompt says "one or two
   short sentences" — short replies are normal; a mid-word cut is not.

### Case D — Caller audio missing / one-way audio

1. `ls -l smoke/calls/ | tail -1` → find the latest call dir.
2. `cat smoke/calls/<call_id>/metadata.json`:
   - `"frame_count": 796` + WAV `> 100 KB` → recording healthy; problem is elsewhere.
   - `"frame_count": 3` + WAV tiny → **inbound audio path broken** → the multi-relay
     fix is missing. Verify in `bridge/third_party/meowcaller/engine_media.go` that
     `connectAndAllocateAll` exists (PR #26). **Do NOT "fix" it back to
     single-relay binding — inbound audio breaks.**
3. `grep -i "relay\|allocate" <bridge log> | tail -10` — relays offered/allocated.

### Case E — `protocol mismatch` on gateway connect

`grep "protocol" <voice-agent log> | tail -5`:
- `protocol=v3` or `protocol=v4` → negotiation OK, ignore.
- `protocol mismatch ... expected=4` + close `1002` → the client advertised only v3.
  The client must advertise a **range**: `minProtocol=3, maxProtocol=4` — v4 servers
  only accept v3 for role=node / mode=probe, general operator clients must reach v4.
  (Fixed in `openclaw_gateway_client.py`; make sure the deployed copy has
  `MIN_PROTOCOL_VERSION = 3` / `MAX_PROTOCOL_VERSION = 4`.)

### Case F — `gateway heartbeat failed ()` reconnect loop

```
WARNING meowcaller.gateway: gateway heartbeat failed (), scheduling reconnect
INFO    meowcaller.gateway: reconnecting to gateway...
INFO    meowcaller.gateway: gateway connected: connId=... 
INFO    meowcaller.gateway: gateway heartbeat started (interval=5s)
WARNING meowcaller.gateway: gateway heartbeat failed ()   ← repeats every ~30-70s
```
- Each cycle reconnects successfully, so calls may still work, but the flapping can
  cause slow/missing `chat.send` replies. Check the **gateway side**:
  `journalctl -u openclaw-gateway --since "10 min ago" | grep -iE "error|memory|oom"` —
  known to correlate with gateway memory pressure (RSS 3+ GB). Restart the gateway
  (`systemctl restart openclaw-gateway`) and watch the loop stop.
- If it keeps looping after a gateway restart, capture the reconnect interval
  pattern (`grep "heartbeat failed" <voice-agent log> | tail -20`) and report it
  with the gateway logs.

### Case G — Bridge down / WhatsApp disconnected

`<bridge>/meowcaller-supervisor.log` — look for:
- `❌ Bridge FAILED — check <logfile>` → read that `restart-bridge-*.log`:
  - `WhatsApp: connected successfully` then silence → WhatsApp session died; check
    the pairing/credentials.
  - `connecting to WhatsApp...` stuck → network blocked to WhatsApp servers.
- Supervisor auto-restarts within ≤15s (CHECK_INTERVAL=15, STARTUP_GRACE=8). Frequent
  restart cycles = config error at boot (e.g. `outgoing.allowlist: []` instead of
  `false` crashes the bridge on start).

### Case H — Call never answered / caller hears rejection

1. `grep "incoming call REJECTED" <bridge log> | tail -5` — present → the incoming
   allowlist guard rejected the caller. Check the config:
   - `incoming.allowlist: true` + caller not in `allowlist_numbers` → **by design**
     (add the caller's number/LID to allow).
   - `incoming.allowlist: true` + empty `allowlist_numbers` → strict mode rejects
     EVERYONE — add at least your own number/LID.
   - `incoming.allowlist: []` → config type error: must be `true`/`false` boolean
     (same crash pitfall as `outgoing.allowlist`).
2. `grep "\[incoming\] allowlist=" <bridge log> | tail -1` — startup line confirms
   what the bridge loaded: `allowlist=true numbers=2`.
3. No REJECTED lines but calls still not answered → check
   `grep "incoming call identity" <bridge log> | tail -3` — if the bridge sees the
   call at all; if not, the problem is WhatsApp-side (session/ringing), not the guard.

## 4. Quick diagnostic one-liners (run in the deployment dir)

```bash
# Overall health
./supervisor/supervisor.sh status                      # both services alive?
tail -20 meowcaller-supervisor.log                     # start/restart history

# Last call, end to end
grep "call started" meowcaller-openclaw-voice.log | tail -1
grep "opening audio played" meowcaller-openclaw-voice.log | tail -1
grep "final transcript" meowcaller-openclaw-voice.log | tail -1
grep "chat.send ack" meowcaller-openclaw-voice.log | tail -1
grep "gateway response" meowcaller-openclaw-voice.log | tail -1
grep "TTS decoded" meowcaller-openclaw-voice.log | tail -1

# Recording health
ls -t smoke/calls/ | head -1
cat smoke/calls/$(ls -t smoke/calls/ | head -1)/metadata.json

# ffmpeg dependency (needed for wacall.py conversions + MP3 greeting decode)
ffmpeg -version | head -1   # "ffmpeg version ..." = OK; "command not found" = install it

# Errors (any of these = problem)
grep -iE "401|403|429|unauthorized|invalid.*key|protocol mismatch|Traceback" meowcaller-openclaw-voice.log | tail -10

# Gateway health
grep "heartbeat failed" meowcaller-openclaw-voice.log | tail -5   # reconnect loop?
grep "gateway connected" meowcaller-openclaw-voice.log | tail -1  # protocol=vX?
```

## 5. Reporting rules (when you ask for help)

Always include:
1. `call_id` + timestamps (from `metadata.json`).
2. The **exact log lines** for that call, in order (copy-paste, don't paraphrase) —
   especially the first missing phase of the golden path (section 2.2).
3. `metadata.json` contents (`frame_count` is the key audio-health number).
4. What you already ruled out (e.g. "STT transcript exists, TTS decoded exists, caller
   still heard nothing").

**Never report "not working" without the log lines. The logs tell the story — read
them first.**
