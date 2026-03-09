#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if [[ -f ".env" ]]; then
  set -a
  source ".env"
  set +a
elif [[ -f "ace/.env" ]]; then
  set -a
  source "ace/.env"
  set +a
fi

GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-${API_BASE_URL:-http://127.0.0.1:8011}}"
FROM_HANDLE="${1:-${LINQ_SMOKE_FROM:-+15551234567}}"
USER_ID="${2:-${LINQ_SMOKE_USER_ID:-}}"
MESSAGE="${3:-${LINQ_SMOKE_MESSAGE:-hello from linq smoke}}"
CONVERSATION_ID="${4:-linq-smoke-$(date +%s)}"
LINK_FIRST="${LINK_FIRST:-1}"

if [[ -z "${USER_ID}" ]]; then
  echo "Pass user_id as arg 2 or set LINQ_SMOKE_USER_ID" >&2
  exit 1
fi

if [[ "${LINK_FIRST}" == "1" ]]; then
  "${SCRIPT_DIR}/gateway_messaging_link.sh" "linq_imessage" "${FROM_HANDLE}" "${USER_ID}" "imessage" >/tmp/gateway_linq_link.json
  cat /tmp/gateway_linq_link.json
  echo
fi

PAYLOAD="$(python3 - "${FROM_HANDLE}" "${MESSAGE}" "${CONVERSATION_ID}" <<'PY'
import json
import sys

from_handle, message, conversation_id = sys.argv[1:4]
print(json.dumps({
    "from": from_handle,
    "text": message,
    "conversation_id": conversation_id,
}))
PY
)"

ARGS=(
  -sS
  -X POST
  "${GATEWAY_BASE_URL}/messaging/linq/imessage"
  -H "Content-Type: application/json"
  -d "${PAYLOAD}"
)

if [[ -n "${LINQ_WEBHOOK_TOKEN:-}" ]]; then
  ARGS+=(-H "X-Linq-Token: ${LINQ_WEBHOOK_TOKEN}")
fi

curl "${ARGS[@]}"
