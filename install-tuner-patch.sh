#!/usr/bin/env bash
#
# install-tuner-patch.sh — Deploy the tuner feature to a running pi-stomp device.
#
# Run from the repo root:
#   ./install-tuner-patch.sh [hostname]
#
# Default hostname: pistomp.local
# You will be prompted for the pistomp password once at the start.

set -euo pipefail

HOST="${1:-pistomp.local}"
USER="pistomp"
REMOTE="${USER}@${HOST}"
REMOTE_DIR="/home/pistomp/pi-stomp"
REMOTE_CONFIG="/home/pistomp/data/config/default_config.yml"
SERVICE="mod-ala-pi-stomp"

# Resolve script directory so scp paths work regardless of cwd
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

info()  { printf "\033[1;34m==> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m    OK: %s\033[0m\n" "$*"; }
fail()  { printf "\033[1;31m    FAIL: %s\033[0m\n" "$*"; exit 1; }

# -------------------------------------------------------------------
# SSH multiplexing — enter password once, reuse for all connections
# -------------------------------------------------------------------
CTRL_SOCK="/tmp/pistomp-deploy-$$"
SSH_OPTS=(-o "ControlPath=${CTRL_SOCK}")

cleanup() {
    ssh -o "ControlPath=${CTRL_SOCK}" -O exit "${REMOTE}" 2>/dev/null || true
}
trap cleanup EXIT

# -------------------------------------------------------------------
# Step 0: Connect (prompts for password once)
# -------------------------------------------------------------------
info "Connecting to ${REMOTE} (enter password when prompted)..."
ssh -o "ControlMaster=yes" -o "ControlPersist=yes" \
    -o "ControlPath=${CTRL_SOCK}" -o "ConnectTimeout=5" \
    "${REMOTE}" "echo connected" \
    || fail "Cannot connect to ${REMOTE}. Check hostname and password."
ok "SSH connected (connection will be reused)"

# -------------------------------------------------------------------
# Step 1: Backup installed code and runtime config
# -------------------------------------------------------------------
info "Creating backup on device..."

TIMESTAMP=$(ssh "${SSH_OPTS[@]}" "${REMOTE}" "date +%Y%m%d-%H%M%S")
BACKUP_FILE="/home/pistomp/pi-stomp-backup-${TIMESTAMP}.tar.gz"

ssh "${SSH_OPTS[@]}" "${REMOTE}" "tar czf ${BACKUP_FILE} -C /home/pistomp pi-stomp/"
ok "Code backed up to ${BACKUP_FILE}"

ssh "${SSH_OPTS[@]}" "${REMOTE}" "cp ${REMOTE_CONFIG} ${REMOTE_CONFIG}.bak"
ok "Config backed up to ${REMOTE_CONFIG}.bak"

# -------------------------------------------------------------------
# Step 2: Stop the service
# -------------------------------------------------------------------
info "Stopping ${SERVICE}..."
ssh "${SSH_OPTS[@]}" "${REMOTE}" "sudo systemctl stop ${SERVICE}"

STATUS=$(ssh "${SSH_OPTS[@]}" "${REMOTE}" "systemctl is-active ${SERVICE} || true")
if [ "${STATUS}" = "inactive" ]; then
    ok "Service stopped"
else
    fail "Service still ${STATUS} after stop"
fi

# -------------------------------------------------------------------
# Step 3: Check Python dependencies
# -------------------------------------------------------------------
info "Checking Python dependencies on device..."

NUMPY_OK=true
JACK_OK=true

ssh "${SSH_OPTS[@]}" "${REMOTE}" "sudo python3 -c 'import numpy; print(\"numpy\", numpy.__version__)'" || NUMPY_OK=false
ssh "${SSH_OPTS[@]}" "${REMOTE}" "sudo python3 -c 'import jack; print(\"jack ok\")'" || JACK_OK=false

if [ "${NUMPY_OK}" = false ] || [ "${JACK_OK}" = false ]; then
    info "Installing missing dependencies..."
    ssh "${SSH_OPTS[@]}" "${REMOTE}" "sudo pip3 install numpy JACK-Client"
    ok "Dependencies installed"
else
    ok "All dependencies present"
fi

# -------------------------------------------------------------------
# Step 4: Deploy changed files (8 modified + 1 new)
# -------------------------------------------------------------------
info "Deploying files to device..."

scp "${SSH_OPTS[@]}" \
    "${SCRIPT_DIR}/pistomp/tuner.py" \
    "${SCRIPT_DIR}/pistomp/analogswitch.py" \
    "${SCRIPT_DIR}/pistomp/footswitch.py" \
    "${SCRIPT_DIR}/pistomp/handler.py" \
    "${SCRIPT_DIR}/pistomp/hardware.py" \
    "${SCRIPT_DIR}/pistomp/lcd320x240.py" \
    "${REMOTE}:${REMOTE_DIR}/pistomp/"

scp "${SSH_OPTS[@]}" \
    "${SCRIPT_DIR}/common/token.py" \
    "${REMOTE}:${REMOTE_DIR}/common/"

scp "${SSH_OPTS[@]}" \
    "${SCRIPT_DIR}/modalapi/modhandler.py" \
    "${REMOTE}:${REMOTE_DIR}/modalapi/"

ok "9 files deployed"

info "Verifying syntax on device..."

ssh "${SSH_OPTS[@]}" "${REMOTE}" "python3 -c 'import py_compile; py_compile.compile(\"${REMOTE_DIR}/pistomp/tuner.py\", doraise=True)'" \
    && ok "tuner.py" || fail "tuner.py syntax error"

ssh "${SSH_OPTS[@]}" "${REMOTE}" "python3 -c 'import py_compile; py_compile.compile(\"${REMOTE_DIR}/modalapi/modhandler.py\", doraise=True)'" \
    && ok "modhandler.py" || fail "modhandler.py syntax error"

ssh "${SSH_OPTS[@]}" "${REMOTE}" "python3 -c 'import py_compile; py_compile.compile(\"${REMOTE_DIR}/pistomp/lcd320x240.py\", doraise=True)'" \
    && ok "lcd320x240.py" || fail "lcd320x240.py syntax error"

ssh "${SSH_OPTS[@]}" "${REMOTE}" "python3 -c 'import py_compile; py_compile.compile(\"${REMOTE_DIR}/pistomp/footswitch.py\", doraise=True)'" \
    && ok "footswitch.py" || fail "footswitch.py syntax error"

# -------------------------------------------------------------------
# Step 5: Patch runtime config
# -------------------------------------------------------------------
info "Patching runtime config..."

# Check if tuner config is already present
if ssh "${SSH_OPTS[@]}" "${REMOTE}" "grep -q 'toggle_tuner' ${REMOTE_CONFIG}"; then
    ok "Config already contains toggle_tuner — skipping"
else
    # Verify FS2 block exists with midi_CC: 62 and no longpress
    if ! ssh "${SSH_OPTS[@]}" "${REMOTE}" "grep -q 'midi_CC: 62' ${REMOTE_CONFIG}"; then
        fail "Cannot find 'midi_CC: 62' in config — manual edit needed"
    fi

    # Insert longpress and longpress_time after midi_CC: 62
    ssh "${SSH_OPTS[@]}" "${REMOTE}" "sed -i '/    midi_CC: 62$/a\\    longpress: toggle_tuner\n    longpress_time: 2.0' ${REMOTE_CONFIG}"

    # Verify the patch landed
    if ssh "${SSH_OPTS[@]}" "${REMOTE}" "grep -q 'toggle_tuner' ${REMOTE_CONFIG}"; then
        ok "Config patched"
    else
        fail "Config patch did not apply"
    fi

    # Validate YAML
    ssh "${SSH_OPTS[@]}" "${REMOTE}" "python3 -c \"import yaml; yaml.safe_load(open('${REMOTE_CONFIG}')); print('YAML OK')\"" \
        || fail "Patched config is not valid YAML"
    ok "YAML validated"
fi

# Show the patched FS2 block
info "FS2 config block:"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "grep -A6 'id: 2' ${REMOTE_CONFIG}"

# -------------------------------------------------------------------
# Step 6: Restart the service
# -------------------------------------------------------------------
info "Starting ${SERVICE}..."
ssh "${SSH_OPTS[@]}" "${REMOTE}" "sudo systemctl start ${SERVICE}"

sleep 3

STATUS=$(ssh "${SSH_OPTS[@]}" "${REMOTE}" "systemctl is-active ${SERVICE} || true")
if [ "${STATUS}" = "active" ]; then
    ok "Service running"
else
    fail "Service is ${STATUS} — check logs with: ssh ${REMOTE} journalctl -u ${SERVICE} -n 50 --no-pager"
fi

info "Recent logs:"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "journalctl -u ${SERVICE} -n 20 --no-pager"

# -------------------------------------------------------------------
# Done
# -------------------------------------------------------------------
echo ""
info "Tuner patch deployed successfully!"
echo ""
echo "  Test on device:"
echo "    1. Verify normal footswitch/encoder/LCD operation"
echo "    2. Hold FS2 (third switch) for 2s — tuner panel appears, audio mutes"
echo "    3. Pluck a string — note name and cent meter respond"
echo "    4. Hold FS2 for 2s again — normal UI restores, audio unmutes"
echo ""
echo "  Monitor logs:  ssh ${REMOTE} journalctl -u ${SERVICE} -f --no-pager"
echo ""
echo "  Rollback:      ssh ${REMOTE} sudo systemctl stop ${SERVICE}"
echo "                 ssh ${REMOTE} \"cd /home/pistomp && tar xzf ${BACKUP_FILE}\""
echo "                 ssh ${REMOTE} cp ${REMOTE_CONFIG}.bak ${REMOTE_CONFIG}"
echo "                 ssh ${REMOTE} sudo systemctl start ${SERVICE}"
