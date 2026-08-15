#!/usr/bin/env bash
# MEOWcaller OpenClaw Voice Agent launcher.
#
# This launches the OpenClaw voice path agent (STT → OpenClaw Gateway → TTS).
# The existing run_converse_agent.sh is NOT modified.
#
# Required env vars:
#   ELEVENLABS_API_KEY       - ElevenLabs API key (for STT + TTS)
#   OPENCLAW_GATEWAY_TOKEN   - OpenClaw gateway auth token
#
# Optional env vars:
#   OPENCLAW_GATEWAY_URL     - Gateway WebSocket URL (default: ws://127.0.0.1:18789)
#   OPENCLAW_GATEWAY_TIMEOUT - Gateway request timeout in seconds (default: 60)
#   MEOWCALLER_BRIDGE_URL    - Bridge WebSocket URL (default: ws://127.0.0.1:9090/ws)
#   ELEVENLABS_TTS_VOICE_ID  - TTS voice ID override
#   ELEVENLABS_TTS_SPEED     - TTS speed (default: 0.94)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/meowcaller-openclaw-voice.log"

# --- Ensure user-local binaries (ffmpeg, etc.) are on PATH ---
# The supervisor launches this script via nohup with a minimal PATH that
# does NOT include ~/.local/bin (where ffmpeg lives on this host).
export PATH="${HOME}/.local/bin:${PATH}"

# --- Auto-load ElevenLabs API key from existing STT wrapper ---
if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
  KEY="$(sed -n 's/^KEY="\([^"]*\)"/\1/p' /opt/wa-call-claw/CUSTOM_SKILL/elevenlabs-stt/elevenlabs_stt.sh | head -1)"
  if [[ -n "${KEY}" ]]; then
    export ELEVENLABS_API_KEY="${KEY}"
    unset KEY
  fi
fi

# --- Auto-load Mimo API key and endpoint from openclaw.json (for LLM fallback) ---
if [[ -z "${MIMO_API_KEY:-}" ]]; then
  MIMO_KEY="$(python3 -c "
import json
with open('/opt/openclaw/openclaw.json') as f:
    cfg = json.load(f)
providers = cfg.get('models', {}).get('providers', {})
xiaomi = providers.get('xiaomi-coding', {})
print(xiaomi.get('apiKey', ''))
" 2>/dev/null)"
  if [[ -n "${MIMO_KEY}" ]]; then
    export MIMO_API_KEY="${MIMO_KEY}"
    unset MIMO_KEY
  fi
fi
export MIMO_BASE_URL="${MIMO_BASE_URL:-https://token-plan-sgp.xiaomimimo.com/v1}"
export MIMO_MODEL="${MIMO_MODEL:-mimo-v2.5}"

# --- Auto-load Gateway token from openclaw.json ---
if [[ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
  GW_TOKEN="$(python3 -c "
import json
with open('/opt/openclaw/openclaw.json') as f:
    cfg = json.load(f)
print(cfg.get('gateway', {}).get('auth', {}).get('token', ''))
" 2>/dev/null)"
  if [[ -n "${GW_TOKEN}" ]]; then
    export OPENCLAW_GATEWAY_TOKEN="${GW_TOKEN}"
    unset GW_TOKEN
  fi
fi

# --- Set device identity path for gateway auth ---
export OPENCLAW_DEVICE_IDENTITY_PATH="${OPENCLAW_DEVICE_IDENTITY_PATH:-/opt/openclaw/identity/device.json}"

# --- TTS voice settings (matches existing MEOWcaller WAV announcement) ---
export ELEVENLABS_TTS_VOICE_ID="${ELEVENLABS_TTS_VOICE_ID:-gmnazjXOFoOcWA59sd5m}"
export ELEVENLABS_TTS_MODEL="${ELEVENLABS_TTS_MODEL:-eleven_flash_v2_5}"
export ELEVENLABS_TTS_SPEED="${ELEVENLABS_TTS_SPEED:-0.94}"
export ELEVENLABS_TTS_STABILITY="${ELEVENLABS_TTS_STABILITY:-0.5}"
export ELEVENLABS_TTS_SIMILARITY_BOOST="${ELEVENLABS_TTS_SIMILARITY_BOOST:-0.5}"
export ELEVENLABS_TTS_STYLE="${ELEVENLABS_TTS_STYLE:-0.5}"

# --- Validate required vars ---
if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
    echo "ERROR: ELEVENLABS_API_KEY could not be loaded" >&2
    exit 1
fi
if [[ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
    echo "ERROR: OPENCLAW_GATEWAY_TOKEN could not be loaded from openclaw.json" >&2
    exit 1
fi

echo "[$(date -Iseconds)] Starting MEOWcaller OpenClaw Voice Agent" >> "$LOG_FILE"
echo "[$(date -Iseconds)] Gateway URL: ${OPENCLAW_GATEWAY_URL:-ws://127.0.0.1:18789}" >> "$LOG_FILE"

PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/meowcaller_openclaw_voice.py" \
    --log-level "${MEOWCALLER_AGENT_LOG_LEVEL:-INFO}" \
    "$@" \
    2>&1 | tee -a "$LOG_FILE"
