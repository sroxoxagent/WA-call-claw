#!/usr/bin/env bash
# =============================================================================
# MEOWcaller Supervisor v3 — SIMPLE (2026-08-15)
#
# Prinsip (per arahan Owner):
#   - Pastikan HANYA 1 supervisor hidup: supervisor BARU langsung KILL
#     supervisor lama yang masih jalan. Gak pakai lock file.
#   - Jaga 2 service tetap hidup: Bridge Go + Voice Agent.
#   - Cek hidup = pgrep simpel (sama kayak cek service biasa).
#   - Gak ada lock, gak ada setsid, gak ribet.
#
# Cara pakai:
#   ./meowcaller_supervisor.sh            # jalan (biasanya via systemd)
#   ./meowcaller_supervisor.sh status     # cek status, tanpa start
# =============================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POC_DIR="${SCRIPT_DIR}"
AGENT_DIR="/opt/wa-call-claw/voice-agent"
SUPERVISOR_LOG="${SCRIPT_DIR}/meowcaller-supervisor.log"
CHECK_INTERVAL=15
STARTUP_GRACE=8

log() { echo "[$(date '+%F %T')] $*" >> "${SUPERVISOR_LOG}"; }

# ---------------------------------------------------------------------------
# Pastikan cuma 1 supervisor: KILL supervisor lain yang masih hidup.
# (Cmdline harus PERSIS bash .../meowcaller_supervisor.sh — jangan sampai
#  ngekill proses lain yang cuma menyebut nama script ini.)
# ---------------------------------------------------------------------------
for p in $(pgrep -f 'meowcaller_supervisor\.sh' 2>/dev/null); do
  [ "${p}" = "$$" ] && continue
  cmd="$(tr '\0' ' ' < "/proc/${p}/cmdline" 2>/dev/null)"
  case "${cmd}" in
    bash*|/bin/bash*|/usr/bin/bash*) ;;
    *) continue ;;   # bukan bash — bukan supervisor
  esac
  echo "${cmd}" | grep -qE 'meowcaller_supervisor\.sh[[:space:]]*$' || continue
  log "⚠️ Another supervisor found (PID ${p}) — KILL"
  kill -9 "${p}" 2>/dev/null
done

# ---------------------------------------------------------------------------
# Cek hidup — pgrep simpel
# ---------------------------------------------------------------------------
is_bridge_alive() { pgrep -f 'meowcaller-poc -config config.yaml' >/dev/null 2>&1; }
is_voice_alive()  { pgrep -f 'meowcaller_openclaw_voice.py' >/dev/null 2>&1; }

start_bridge() {
  log "🚀 Starting Bridge Go..."
  local logfile="${POC_DIR}/restart-bridge-$(date +%Y%m%d-%H%M).log"
  # exec: anak langsung jadi meowcaller-poc (PID sama) → di-reparent ke
  # systemd saat subshell exit. Gak ada wrapper bash yang nyangkut.
  ( cd "${POC_DIR}" && exec ./meowcaller-poc -config config.yaml > "${logfile}" 2>&1 < /dev/null & echo $! > "${POC_DIR}/.meowcaller-bridge.pid" )
  sleep "${STARTUP_GRACE}"
  is_bridge_alive && log "✅ Bridge up" || log "❌ Bridge FAILED — check ${logfile}"
}

start_voice() {
  log "🚀 Starting Voice Agent..."
  ( cd "${AGENT_DIR}" && exec bash run_openclaw_voice.sh > /dev/null 2>&1 < /dev/null & echo $! > "${POC_DIR}/.meowcaller-voice.pid" )
  sleep "${STARTUP_GRACE}"
  # The wrapper PID above is the bash parent; run_openclaw_voice.sh ends in
  # an exec+tee pipeline so the real python PID differs. Store the python
  # PID so manual restarts (kill $(cat .meowcaller-voice.pid)) hit the
  # actual agent process.
  VPID=$(pgrep -f 'meowcaller_openclaw_voice\.py' | head -1)
  if [ -n "${VPID}" ]; then
    echo "${VPID}" > "${POC_DIR}/.meowcaller-voice.pid"
  fi
  is_voice_alive && log "✅ Voice Agent up" || log "❌ Voice Agent FAILED — check ${AGENT_DIR}/meowcaller-openclaw-voice.log"
}

# --- Mode status ---
if [ "${1:-}" = "status" ]; then
  echo "Supervisor PID : $$"
  echo "Bridge  : $(is_bridge_alive && echo UP || echo DOWN)"
  echo "Voice   : $(is_voice_alive && echo UP || echo DOWN)"
  exit 0
fi

log "🟢 MEOWcaller Supervisor v3 started (PID $$)"

while true; do
  is_bridge_alive || start_bridge
  is_voice_alive  || start_voice
  sleep "${CHECK_INTERVAL}"
done
