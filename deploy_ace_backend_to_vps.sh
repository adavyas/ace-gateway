#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
LOCAL_BACKEND_DIR="${PROJECT_ROOT}"

VPS_HOST="${VPS_HOST:-15.204.88.57}"
VPS_USER="${VPS_USER:-ubuntu}"
VPS_SSH_KEY="${VPS_SSH_KEY:-${HOME}/.ssh/id_ed25519}"

ACE_VPS_BACKEND_DIR="${ACE_VPS_BACKEND_DIR:-/opt/ace/backend}"
ACE_VPS_COMPOSE_FILE="${ACE_VPS_COMPOSE_FILE:-docker-compose.vps.yml}"
ACE_VPS_GATEWAY_SERVICE="${ACE_VPS_GATEWAY_SERVICE:-gateway}"
ACE_VPS_MANAGER_SERVICE="${ACE_VPS_MANAGER_SERVICE:-ace-net-manager}"
ACE_VPS_DOCKER_NETWORK="${ACE_VPS_DOCKER_NETWORK:-ace-net}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

if [[ ! -d "${LOCAL_BACKEND_DIR}" ]]; then
  echo "Local backend directory not found: ${LOCAL_BACKEND_DIR}" >&2
  exit 1
fi

if [[ ! -f "${LOCAL_BACKEND_DIR}/${ACE_VPS_COMPOSE_FILE}" ]]; then
  echo "Expected ${LOCAL_BACKEND_DIR}/${ACE_VPS_COMPOSE_FILE} to exist." >&2
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
docker network inspect "${ACE_VPS_DOCKER_NETWORK}" >/dev/null 2>&1 || docker network create "${ACE_VPS_DOCKER_NETWORK}"
docker compose -f "${ACE_VPS_COMPOSE_FILE}" up -d --build "${ACE_VPS_GATEWAY_SERVICE}" "${ACE_VPS_MANAGER_SERVICE}"
docker compose -f "${ACE_VPS_COMPOSE_FILE}" ps
EOF

echo "Bootstrapping compose services on VPS"
ssh "${SSH_ARGS[@]}" "${REMOTE_BOOTSTRAP}"

echo "Deployment complete."
