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
FROM_NUMBER="${1:-${TWILIO_SMOKE_FROM:-+15551234567}}"
USER_ID="${2:-${TWILIO_SMOKE_USER_ID:-}}"
MESSAGE="${3:-${TWILIO_SMOKE_MESSAGE:-hello from twilio smoke}}"
LINK_FIRST="${LINK_FIRST:-1}"
WEBHOOK_URL="${GATEWAY_BASE_URL}/messaging/twilio/whatsapp"

if [[ -z "${USER_ID}" ]]; then
  echo "Pass user_id as arg 2 or set TWILIO_SMOKE_USER_ID" >&2
  exit 1
fi

if [[ "${LINK_FIRST}" == "1" ]]; then
  "${SCRIPT_DIR}/gateway_messaging_link.sh" "twilio_whatsapp" "${FROM_NUMBER}" "${USER_ID}" "whatsapp" >/tmp/gateway_twilio_link.json
  cat /tmp/gateway_twilio_link.json
  echo
fi

SIGNATURE="$(
python3 - "${WEBHOOK_URL}" "${TWILIO_AUTH_TOKEN:-}" "${FROM_NUMBER}" "${MESSAGE}" <<'PY'
import base64
import hashlib
import hmac
import sys

url, auth_token, from_number, message = sys.argv[1:5]
if not auth_token:
    print("")
    raise SystemExit(0)
items = [("Body", message), ("From", f"whatsapp:{from_number}")]
payload = [url]
for key, value in sorted(items):
    payload.append(key)
    payload.append(value)
digest = hmac.new(auth_token.encode("utf-8"), "".join(payload).encode("utf-8"), hashlib.sha1).digest()
print(base64.b64encode(digest).decode("utf-8"))
PY
)"

ARGS=(
  -sS
  -X POST
  "${WEBHOOK_URL}"
  -H "Content-Type: application/x-www-form-urlencoded"
  --data-urlencode "From=whatsapp:${FROM_NUMBER}"
  --data-urlencode "Body=${MESSAGE}"
)

if [[ -n "${SIGNATURE}" ]]; then
  ARGS+=(-H "X-Twilio-Signature: ${SIGNATURE}")
fi

curl "${ARGS[@]}"
