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
INTERNAL_TOKEN="${ACE_INTERNAL_API_TOKEN:-${INTERNAL_API_TOKEN:-}}"
PROVIDER="${1:-}"
ADDRESS="${2:-}"
USER_ID="${3:-}"
CHANNEL="${4:-}"

if [[ -z "${PROVIDER}" || -z "${ADDRESS}" || -z "${USER_ID}" ]]; then
  echo "Usage: $0 <provider> <address> <user_id> [channel]" >&2
  echo "Providers: twilio_whatsapp | linq_imessage" >&2
  exit 1
fi

if [[ -z "${INTERNAL_TOKEN}" ]]; then
  echo "ACE_INTERNAL_API_TOKEN or INTERNAL_API_TOKEN must be set" >&2
  exit 1
fi

PAYLOAD="$(python3 - "${PROVIDER}" "${ADDRESS}" "${USER_ID}" "${CHANNEL}" <<'PY'
import json
import sys

provider, address, user_id, channel = sys.argv[1:5]
payload = {
    "provider": provider,
    "address": address,
    "user_id": user_id,
}
if channel:
    payload["channel"] = channel
print(json.dumps(payload))
PY
)"

curl -sS -X POST "${GATEWAY_BASE_URL}/messaging/admin/link" \
  -H "Authorization: Bearer ${INTERNAL_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}"
