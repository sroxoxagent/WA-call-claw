#!/usr/bin/env bash
# Launch the MEOWcaller -> ElevenLabs Realtime STT agent.
# The key is loaded from the existing ElevenLabs STT wrapper; it is never printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/meowcaller-stt-agent.log"

# Existing workspace STT wrapper is the source of truth for the configured key.
KEY="$(sed -n 's/^KEY="\([^"]*\)"/\1/p' /opt/wa-call-claw/CUSTOM_SKILL/elevenlabs-stt/elevenlabs_stt.sh | head -1)"
if [[ -z "${KEY}" ]]; then
  echo "Could not load ElevenLabs key from existing STT wrapper" >&2
  exit 1
fi
export ELEVENLABS_API_KEY="${KEY}"
unset KEY

exec python3 "${SCRIPT_DIR}/elevenlabs_stt_agent.py" "$@" 2>&1 | tee -a "${LOG_FILE}"
