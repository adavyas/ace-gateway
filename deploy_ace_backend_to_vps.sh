#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCAL_BACKEND_DIR="${PROJECT_ROOT}/ace"

VPS_HOST="${VPS_HOST:-15.204.88.57}"
VPS_USER="${VPS_USER:-ubuntu}"
VPS_SSH_KEY="${VPS_SSH_KEY:-${HOME}/.ssh/id_ed25519}"

ACE_VPS_BACKEND_DIR="${ACE_VPS_BACKEND_DIR:-/opt/ace/backend}"
ACE_VPS_SERVICE_NAME="${ACE_VPS_SERVICE_NAME:-ace-backend}"
ACE_VPS_PORT="${ACE_VPS_PORT:-7777}"
ACE_VPS_WORKERS="${ACE_VPS_WORKERS:-1}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

if [[ ! -d "${LOCAL_BACKEND_DIR}" ]]; then
  echo "Local backend directory not found: ${LOCAL_BACKEND_DIR}" >&2
  exit 1
fi

if [[ ! -f "${LOCAL_BACKEND_DIR}/main.py" ]]; then
  echo "Expected ${LOCAL_BACKEND_DIR}/main.py to exist." >&2
  exit 1
fi

SSH_ARGS=(
  -i "${VPS_SSH_KEY}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  "${VPS_USER}@${VPS_HOST}"
)

RSYNC_ARGS=(
  -az
  --delete
  --exclude ".venv"
  --exclude "__pycache__"
  --exclude ".pytest_cache"
  --exclude "*.pyc"
)

if [[ "${DRY_RUN}" -eq 1 ]]; then
  RSYNC_ARGS+=(--dry-run)
fi

echo "Syncing backend to ${VPS_USER}@${VPS_HOST}:${ACE_VPS_BACKEND_DIR}"
ssh "${SSH_ARGS[@]}" "sudo mkdir -p '${ACE_VPS_BACKEND_DIR}' && sudo chown -R '${VPS_USER}:${VPS_USER}' '${ACE_VPS_BACKEND_DIR}'"
rsync "${RSYNC_ARGS[@]}" "${LOCAL_BACKEND_DIR}/" "${VPS_USER}@${VPS_HOST}:${ACE_VPS_BACKEND_DIR}/"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "Dry run complete."
  exit 0
fi

read -r -d '' REMOTE_BOOTSTRAP <<EOF || true
set -euo pipefail
sudo mkdir -p "${ACE_VPS_BACKEND_DIR}"
sudo chown -R "${VPS_USER}:${VPS_USER}" "${ACE_VPS_BACKEND_DIR}"
cd "${ACE_VPS_BACKEND_DIR}"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
if [[ -f requirements.prod.txt ]]; then
  pip install -r requirements.prod.txt
else
  pip install -r requirements.txt
fi

cat <<UNIT | sudo tee /etc/systemd/system/${ACE_VPS_SERVICE_NAME}.service >/dev/null
[Unit]
Description=ACE Backend API
After=network.target

[Service]
Type=simple
User=${VPS_USER}
WorkingDirectory=${ACE_VPS_BACKEND_DIR}
Environment=PORT=${ACE_VPS_PORT}
ExecStart=${ACE_VPS_BACKEND_DIR}/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${ACE_VPS_PORT} --workers ${ACE_VPS_WORKERS}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now ${ACE_VPS_SERVICE_NAME}
sudo systemctl restart ${ACE_VPS_SERVICE_NAME}
sudo systemctl status ${ACE_VPS_SERVICE_NAME} --no-pager --lines=30
EOF

echo "Bootstrapping service ${ACE_VPS_SERVICE_NAME} on VPS"
ssh "${SSH_ARGS[@]}" "${REMOTE_BOOTSTRAP}"

echo "Deployment complete."
