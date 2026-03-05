#!/usr/bin/env bash
# start_hermes_server.sh
#
# Run this script ON THE VPS to start the Hermes Agent HTTP server.
#
# The server listens on 127.0.0.1:7777 (loopback only) so it is only
# reachable through the SSH tunnel opened by the Scope backend.
#
# Usage:
#   chmod +x start_hermes_server.sh
#   ./start_hermes_server.sh
#
# To run as a background daemon (with logging):
#   nohup ./start_hermes_server.sh >> ~/hermes_server.log 2>&1 &
#
# Environment variables (optional overrides):
#   HERMES_PORT        - TCP port to bind (default: 7777)
#   HERMES_WORKERS     - Number of uvicorn workers (default: 1)
#   HERMES_LOG_LEVEL   - Uvicorn log level: debug|info|warning|error (default: info)
#   HERMES_CLI_CMD     - JSON array of the CLI command (default: '["hermes"]')
#                        e.g. '["python", "-m", "hermes.cli"]'

set -euo pipefail

# -------------------------------------------------------------------------
# Defaults
# -------------------------------------------------------------------------
HERMES_PORT="${HERMES_PORT:-7777}"
HERMES_WORKERS="${HERMES_WORKERS:-1}"
HERMES_LOG_LEVEL="${HERMES_LOG_LEVEL:-info}"

# -------------------------------------------------------------------------
# Activate virtual environment if present (common VPS setups)
# -------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${SCRIPT_DIR}/.venv/bin/activate" ]]; then
    echo "[hermes] Activating .venv in ${SCRIPT_DIR}"
    # shellcheck source=/dev/null
    source "${SCRIPT_DIR}/.venv/bin/activate"
elif [[ -f "${SCRIPT_DIR}/venv/bin/activate" ]]; then
    echo "[hermes] Activating venv in ${SCRIPT_DIR}"
    # shellcheck source=/dev/null
    source "${SCRIPT_DIR}/venv/bin/activate"
elif [[ -n "${VIRTUAL_ENV:-}" ]]; then
    echo "[hermes] Using already-active virtual environment: ${VIRTUAL_ENV}"
else
    echo "[hermes] No virtual environment found; using system Python."
fi

# -------------------------------------------------------------------------
# Confirm hermes CLI is reachable (warn but don't abort)
# -------------------------------------------------------------------------
if ! command -v hermes &>/dev/null; then
    echo "[hermes] WARNING: 'hermes' command not found on PATH."
    echo "         Set HERMES_CLI_CMD to point at the correct executable."
    echo "         e.g.  export HERMES_CLI_CMD='[\"python\",\"-m\",\"hermes.cli\"]'"
fi

# -------------------------------------------------------------------------
# Start the server
# -------------------------------------------------------------------------
echo "[hermes] Starting Hermes Agent HTTP server on 127.0.0.1:${HERMES_PORT} ..."
echo "[hermes] Workers: ${HERMES_WORKERS}  |  Log level: ${HERMES_LOG_LEVEL}"

exec uvicorn app.routers.hermes_server:app \
    --host 127.0.0.1 \
    --port "${HERMES_PORT}" \
    --workers "${HERMES_WORKERS}" \
    --log-level "${HERMES_LOG_LEVEL}" \
    --no-access-log
