# Supervisor

Keeps the two critical processes of the voice agent alive — if either dies, it is restarted automatically within seconds.

## What it watches

| Component | Default command | Default process pattern |
|-----------|-----------------|-------------------------|
| Go bridge | `./meowcaller-poc -config config.yaml` | `meowcaller-poc -config config.yaml` |
| Python voice agent | `bash run_openclaw_voice.sh` | `meowcaller_openclaw_voice.py` |

## Usage

```bash
chmod +x supervisor.sh

# Foreground
./supervisor.sh

# Background (survives logout, not reboot)
nohup ./supervisor.sh > /dev/null 2>&1 &
```

Stop the supervisor any time — **the watched processes are NOT killed**:

```bash
kill $(cat supervisor.lock)
```

## Features

1. **Auto-restart** — a dead bridge or voice agent comes back within ≤15s.
2. **Anti-duplicate** — `flock`-based lock file; a second supervisor exits immediately.
3. **Anti restart-storm** — if a component restarts >3× within 5 minutes, a 60s cooldown kicks in (prevents crash loops from burning CPU / spamming logs).
4. **Full logging** — `supervisor.log` records every start/stop/restart, plus per-restart logs (`bridge-YYYYMMDD-HHMM.log`, `voice-agent-YYYYMMDD-HHMM.log`).

## Configuration

Everything is configurable via environment variables — no code edits:

| Variable | Default | Purpose |
|----------|---------|---------|
| `BRIDGE_DIR` | `../bridge` (relative to script) | Folder of the Go bridge |
| `AGENT_DIR` | `../agent` (relative to script) | Folder of the Python agent |
| `BRIDGE_CMD` | `./meowcaller-poc -config config.yaml` | Command to start the bridge |
| `BRIDGE_PATTERN` | same as `BRIDGE_CMD` | `pgrep` pattern to detect a running bridge |
| `AGENT_CMD` | `bash run_openclaw_voice.sh` | Command to start the voice agent |
| `AGENT_PATTERN` | `meowcaller_openclaw_voice.py` | `pgrep` pattern to detect a running agent |
| `SUPERVISOR_LOG` | `supervisor.log` (script folder) | Log file path |
| `CHECK_INTERVAL` | `15` | Seconds between checks |
| `STARTUP_GRACE` | `5` | Seconds to wait after start before verifying |
| `MAX_RESTARTS` | `3` | Max restarts per component per window |
| `COOLDOWN_WINDOW` | `300` | Restart window in seconds |
| `COOLDOWN_SLEEP` | `60` | Cooldown pause in seconds |

Example:

```bash
BRIDGE_DIR=/opt/wa-call-claw/bridge \
AGENT_DIR=/opt/wa-call-claw/agent \
CHECK_INTERVAL=10 \
nohup ./supervisor.sh > /dev/null 2>&1 &
```

## Updating scripts / binaries

The supervisor **does not reload changed files** — running processes keep the old version until restarted.

1. **Bridge binary**: replace the file, then `pkill -f "meowcaller-poc -config config.yaml"`. The supervisor detects the death within 15s and starts the new binary.
2. **Voice agent (.py)**: edit the file, then `pkill -f meowcaller_openclaw_voice.py`. Same auto-restart applies.
3. **Supervisor itself**: edit the script, then `kill $(cat supervisor.lock)` and start it again.

## Optional: systemd user service (survives reboot)

```ini
# ~/.config/systemd/user/wa-call-claw-supervisor.service
[Unit]
Description=WA-call-claw supervisor

[Service]
Type=simple
ExecStart=/absolute/path/to/supervisor/supervisor.sh
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now wa-call-claw-supervisor
systemctl --user status wa-call-claw-supervisor
```
