#!/usr/bin/env bash
# Launch the MEOWcaller conversational agent (STT + LLM + TTS).
# Keys are loaded from existing workspace config; never printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/meowcaller-converse-agent.log"

# --- ElevenLabs API key from existing STT wrapper ---
KEY="$(sed -n 's/^KEY="\([^"]*\)"/\1/p' /opt/wa-call-claw/CUSTOM_SKILL/elevenlabs-stt/elevenlabs_stt.sh | head -1)"
if [[ -z "${KEY}" ]]; then
  echo "Could not load ElevenLabs key from existing STT wrapper" >&2
  exit 1
fi
export ELEVENLABS_API_KEY="${KEY}"
unset KEY

# --- Mimo v2.5 LLM credentials (from openclaw.json) ---
if [[ -z "${MIMO_API_KEY:-}" ]]; then
  # Read from openclaw.json — the xiaomi-coding provider's apiKey
  MIMO_KEY="$(python3 - <<'PY'
import json
with open('/opt/openclaw/openclaw.json') as f:
    cfg = json.load(f)
providers = cfg.get('models', {}).get('providers', {})
xiaomi = providers.get('xiaomi-coding', {})
print(xiaomi.get('apiKey', ''))
PY
  )"
  if [[ -z "${MIMO_KEY}" ]]; then
    echo "Could not load Mimo API key from openclaw.json" >&2
    exit 1
  fi
  export MIMO_API_KEY="${MIMO_KEY}"
  unset MIMO_KEY
fi

export MIMO_BASE_URL="${MIMO_BASE_URL:-https://token-plan-sgp.xiaomimimo.com/v1}"
export MIMO_MODEL="${MIMO_MODEL:-mimo-v2.5}"

# --- Optional TTS voice override (matches existing MEOWcaller WAV announcement) ---
export ELEVENLABS_TTS_VOICE_ID="${ELEVENLABS_TTS_VOICE_ID:-gmnazjXOFoOcWA59sd5m}"
export ELEVENLABS_TTS_MODEL="${ELEVENLABS_TTS_MODEL:-eleven_multilingual_v2}"
export ELEVENLABS_TTS_SPEED="${ELEVENLABS_TTS_SPEED:-0.94}"
export ELEVENLABS_TTS_STABILITY="${ELEVENLABS_TTS_STABILITY:-0.5}"
export ELEVENLABS_TTS_SIMILARITY_BOOST="${ELEVENLABS_TTS_SIMILARITY_BOOST:-0.5}"
export ELEVENLABS_TTS_STYLE="${ELEVENLABS_TTS_STYLE:-0.5}"

PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Missing ${PYTHON_BIN}; install requirements into the agent venv first" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/meowcaller_converse_agent.py" "$@" 2>&1 | tee -a "${LOG_FILE}"
