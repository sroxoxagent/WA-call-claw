#!/usr/bin/env bash
# =============================================================================
# deploy.sh — SINKRONKAN repo → TMP deployment + build + restart bridge
#
# SINGLE SOURCE OF TRUTH = repo ini (WA-call-claw)
#   bridge/       → TMP/meowcaller-poc     (Go bridge runtime)
#   supervisor/   → TMP/meowcaller-poc     (supervisor v3)
#   scripts/      → TMP/meowcaller-poc/scripts
#   voice-agent/  → TMP/meowcaller-agent   (Python voice agent runtime)
#
# TMP = deployment dir (binary + config + log yang dipakai runtime).
# Config AKTIF di TMP TIDAK pernah ditimpa (config.yaml / config.json).
#
# Alur:
#   1. rsync source dari repo → TMP. TANPA --delete supaya file runtime aman.
#   2. go build di TMP → binary baru
#   3. kill bridge → supervisor (v3) otomatis restart dalam <=15 detik
#   4. verifikasi: bridge up + line allowlist di log startup
#
# Cara pakai:
#   ./scripts/deploy.sh          # deploy + build + restart + verifikasi
#   ./scripts/deploy.sh --dry    # cek file apa saja yang akan tersync (tanpa eksekusi)
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="/opt/wa-call-claw"
AGENT_DIR="/opt/wa-call-claw/voice-agent"
LOG="${TMP}/deploy.log"

DRY=""
if [[ "${1:-}" == "--dry" ]]; then DRY="--dry-run"; fi

log() { echo "[$(date '+%F %T')] $*" | tee -a "${LOG}"; }

log "=== DEPLOY $(git -C "${REPO}" rev-parse --short HEAD) ($(git -C "${REPO}" log -1 --format=%s)) ==="

if [[ ! -d "${TMP}" ]]; then
  log "❌ TMP dir tidak ada: ${TMP}"
  exit 1
fi

# --- 1. sync source repo → TMP (satu arah) ---------------------------------
# bridge/ → TMP (Go source; config.yaml aktif TIDAK ditimpa)
rsync -av ${DRY} \
  --exclude '*.log' \
  --exclude 'meowcaller-poc' \
  --exclude '*.bak-*' \
  --exclude 'bin/' \
  --exclude 'smoke/events/' \
  --exclude '__pycache__/' \
  --exclude '.gitignore' \
  --exclude 'README.md' \
  --exclude 'config.yaml.example' \
  "${REPO}/bridge/" "${TMP}/" 2>&1 | tail -15 | tee -a "${LOG}"

# supervisor v3 → TMP
rsync -av ${DRY} "${REPO}/supervisor/supervisor.sh" "${TMP}/meowcaller_supervisor.sh" 2>&1 | tail -3 | tee -a "${LOG}"

# voice-agent → TMP/meowcaller-agent (config.json aktif TIDAK ditimpa)
if [[ -d "${AGENT_DIR}" ]]; then
  rsync -av ${DRY} \
    --exclude '*.log' \
    --exclude '*.pid' \
    --exclude 'backup-*' \
    --exclude '__pycache__/' \
    --exclude '*.bak-*' \
    --exclude 'debug-*.json' \
    --exclude 'config.json' \
    --exclude 'voice_context_config.json' \
    --exclude 'config.json.example' \
    --exclude 'README.md' \
    "${REPO}/voice-agent/" "${AGENT_DIR}/" 2>&1 | tail -15 | tee -a "${LOG}"
fi

if [[ -n "${DRY}" ]]; then
  log "✅ Dry-run selesai — tidak ada yang dieksekusi. Jalankan tanpa --dry untuk deploy."
  exit 0
fi

# --- 2. build di TMP --------------------------------------------------------
log "Building binary..."
cd "${TMP}"
if ! go build -o meowcaller-poc ./cmd/meowcaller-poc 2>&1 | tee -a "${LOG}"; then
  log "❌ BUILD GAGAL — bridge TIDAK di-restart (binary lama tetap jalan)"
  exit 1
fi
log "✅ Build OK: $(ls -la meowcaller-poc | awk '{print $5" bytes"}')"

# --- 3. restart bridge (supervisor auto-restart) ---------------------------
log "Restarting bridge..."
pkill -f "meowcaller-poc -config config.yaml" 2>/dev/null || true
sleep 18

# --- 4. verifikasi ----------------------------------------------------------
BRIDGE_PID="$(pgrep -f '[m]eowcaller-poc -config config.yaml' | head -1)"
if [[ -n "${BRIDGE_PID}" ]]; then
  LATEST_LOG="$(ls -t "${TMP}"/restart-bridge-*.log | head -1)"
  ALLOWLINE="$(grep -o 'allowlist=[a-z]*' "${LATEST_LOG}" | head -1 || echo '?')"
  log "✅ Bridge UP (PID ${BRIDGE_PID}) | ${ALLOWLINE} | log: ${LATEST_LOG}"
else
  log "❌ Bridge TIDAK UP setelah 18s — cek ${TMP}/meowcaller-supervisor.log"
  exit 1
fi

# Voice agent: kalau mati, supervisor juga restart otomatis — cek saja
VOICE_PID="$(pgrep -f '[m]eowcaller_openclaw_voice.py' | head -1)"
if [[ -n "${VOICE_PID}" ]]; then
  log "✅ Voice agent UP (PID ${VOICE_PID})"
else
  log "⚠️ Voice agent belum up (supervisor akan nyalakan dalam <=15s)"
fi

log "=== DEPLOY SELESAI ==="
