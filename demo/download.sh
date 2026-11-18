#!/bin/sh
# Demonstration: a long download on the old generation survives a switch until
# its 5s drain window expires, while a new download immediately uses the new
# target. Requires the compose stack (see README).
#
# When the relay runs with RELAY_PROTECT_DOWNLOADS=1 (the compose default),
# every download first mints a grant via POST /api/download-grants. The grant
# dies with its generation: after the switch the pre-switch grant is rejected
# (403) and the new download needs a freshly minted one.
set -eu

RELAY=${RELAY:-http://localhost:8080}
# Must match the relay's RELAY_ADMIN_KEY when protection mode is on;
# the compose stack ships this local example value.
ADMIN_KEY=${ADMIN_KEY:-dev-admin-key-change-me}

json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

echo "== current target =="
STATUS=$(curl -sS "$RELAY/api/status")
echo "$STATUS" | json_field '["current"]'
PROTECTION=$(echo "$STATUS" | json_field '["protection"]["enabled"]')

TOKEN=""
mint_grant() { # $1=path $2=query -> prints a fresh grant token
  curl -sS -X POST "$RELAY/api/download-grants" \
    -H "authorization: Bearer $ADMIN_KEY" -H 'content-type: application/json' \
    -d "{\"path\":\"$1\",\"query\":\"$2\",\"method\":\"GET\"}" | json_field '["grant"]'
}
download() { # $1=url $2=outfile $3=hdrfile — uses TOKEN when set
  if [ -n "$TOKEN" ]; then
    curl -sS -H "authorization: Bearer $TOKEN" -D "$3" -o "$2" "$1"
  else
    curl -sS -D "$3" -o "$2" "$1"
  fi
}

OLD_QUERY='bytes=6291456&chunk=32768&delay=0.4'
NEW_QUERY='bytes=262144&chunk=65536&delay=0'

if [ "$PROTECTION" = "True" ]; then
  echo "== protection mode on: minting a grant for the slow download =="
  TOKEN=$(mint_grant /artifact "$OLD_QUERY")
  # Minted now but used only after the switch: must be rejected (stale generation).
  STALE_TOKEN=$(mint_grant /artifact "$NEW_QUERY")
fi

echo "== starting slow 6 MiB download through the relay =="
# 6 MiB in 32 KiB chunks, 0.4s apart => roughly 75s.
download "$RELAY/artifact?$OLD_QUERY" /tmp/relay-old.bin /tmp/relay-old.hdr &
OLD_PID=$!
sleep 1

echo "== switching to the other target (drain window 5s) =="
OTHER=$([ "$(curl -sS "$RELAY/api/targets" | json_field '["current"]')" = blue ] && echo green || echo blue)
curl -sS -X POST "$RELAY/api/switch" -H 'content-type: application/json' \
  -d "{\"target\":\"$OTHER\",\"wait\":true,\"wait_timeout\":6}" | python3 -m json.tool

if [ "$PROTECTION" = "True" ]; then
  echo "== the pre-switch grant for the new URL is stale now (expect 403) =="
  CODE=$(curl -sS -o /tmp/relay-stale.json -w '%{http_code}' \
    -H "authorization: Bearer $STALE_TOKEN" "$RELAY/artifact?$NEW_QUERY")
  echo "relay answered $CODE: $(cat /tmp/relay-stale.json)"
  echo "== minting a fresh grant on the new generation =="
  TOKEN=$(mint_grant /artifact "$NEW_QUERY")
else
  TOKEN=""
fi

echo "== new download uses the new target immediately =="
download "$RELAY/artifact?$NEW_QUERY" /tmp/relay-new.bin /tmp/relay-new.hdr
grep -i '^x-relay-target\|^x-upstream' /tmp/relay-new.hdr || true

echo "== waiting for the old download (expect truncation ~5s after the switch) =="
START=$(date +%s)
if wait "$OLD_PID"; then
  echo "old download completed (unexpected for a ~75s body)"
else
  echo "old download interrupted as designed, $(( $(date +%s) - START ))s after the switch"
fi
ls -l /tmp/relay-old.bin /tmp/relay-new.bin

echo "== final status =="
curl -sS "$RELAY/api/status" | python3 -m json.tool
