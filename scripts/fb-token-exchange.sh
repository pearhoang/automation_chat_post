#!/usr/bin/env bash
# =============================================================================
# fb-token-exchange.sh
#
# Exchange a short-lived Graph API user token for a never-expire page token,
# verify it, then write FB_* values into /opt/fb-webhook/.env and restart the
# webhook service.
#
# Inputs (env vars or interactive prompts):
#   FB_APP_ID, FB_APP_SECRET, FB_PAGE_ID,
#   FB_SHORT_LIVED_USER_TOKEN
#
# Optional:
#   FB_GRAPH_VERSION   default v21.0
#   ENV_FILE           default /opt/fb-webhook/.env
#   FB_VERIFY_TOKEN    if unset, a random 32-byte hex is generated
#   SUBSCRIBE_FIELDS   comma-separated; default: messages,messaging_postbacks,feed
#
# Usage:
#   FB_APP_ID=... FB_APP_SECRET=... FB_PAGE_ID=... \
#       FB_SHORT_LIVED_USER_TOKEN=EAAB... bash fb-token-exchange.sh
#
# Or run with no env to be prompted.
#
# Requires: curl, jq, openssl
# =============================================================================
set -euo pipefail

GREEN='\033[1;32m'; CYAN='\033[1;36m'; RED='\033[1;31m'; NC='\033[0m'
log() { printf "${CYAN}==> %s${NC}\n" "$*"; }
ok()  { printf "${GREEN}OK  %s${NC}\n" "$*"; }
die() { printf "${RED}ERR %s${NC}\n" "$*" >&2; exit 1; }

command -v curl >/dev/null || die "curl missing"
command -v jq   >/dev/null || { apt-get install -y -qq jq 2>/dev/null || die "install jq"; }

prompt_if_empty() {
  local name="$1"; local val="${!name:-}"
  if [ -z "$val" ]; then
    read -rp "$name: " val
    eval "$name=\"\$val\""
  fi
}

prompt_if_empty FB_APP_ID
prompt_if_empty FB_APP_SECRET
prompt_if_empty FB_PAGE_ID
prompt_if_empty FB_SHORT_LIVED_USER_TOKEN

FB_GRAPH_VERSION="${FB_GRAPH_VERSION:-v21.0}"
ENV_FILE="${ENV_FILE:-/opt/fb-webhook/.env}"
SUBSCRIBE_FIELDS="${SUBSCRIBE_FIELDS:-messages,messaging_postbacks,feed,messaging_referrals,message_deliveries}"
FB_VERIFY_TOKEN="${FB_VERIFY_TOKEN:-$(openssl rand -hex 32)}"

GRAPH="https://graph.facebook.com/${FB_GRAPH_VERSION}"

# ---------------------------------------------------------------------------
log "Step 0/4 — Validate scopes on the input token"
REQUIRED_SCOPES=(pages_messaging pages_manage_posts pages_manage_metadata
                 pages_manage_engagement pages_read_engagement pages_show_list)
DBG_IN=$(curl -sG "${GRAPH}/debug_token" \
  --data-urlencode "input_token=${FB_SHORT_LIVED_USER_TOKEN}" \
  --data-urlencode "access_token=${FB_APP_ID}|${FB_APP_SECRET}")
if echo "$DBG_IN" | jq -e '.data.error' >/dev/null 2>&1 \
   || echo "$DBG_IN" | jq -e '.error' >/dev/null 2>&1; then
  echo "$DBG_IN" | jq .
  die "input token failed debug_token check (wrong app/secret or expired token?)"
fi
GOT_SCOPES=$(echo "$DBG_IN" | jq -r '.data.scopes | join(" ")')
MISSING=()
for s in "${REQUIRED_SCOPES[@]}"; do
  case " $GOT_SCOPES " in *" $s "*) : ;; *) MISSING+=("$s") ;; esac
done
if [ ${#MISSING[@]} -gt 0 ]; then
  printf "${RED}Missing scopes: %s${NC}\n" "${MISSING[*]}"
  cat <<EOF

The short-lived user token does NOT have all the scopes the webhook needs.
Steps to fix:

  1. Open https://developers.facebook.com/tools/explorer/
  2. Pick your app, click "Add a Permission" and tick:
        ${MISSING[@]}
     (keep the ones you already had ticked)
  3. Click "Generate Access Token" — you must regenerate;
     ticking a scope on the old token does not retroactively grant it.
  4. Copy the new token and re-run this script.

EOF
  die "abort — missing scopes"
fi
ok "scopes OK: ${GOT_SCOPES}"

# ---------------------------------------------------------------------------
log "Step 1/4 — Short-lived → Long-lived user token (60 days)"
LL_RESP=$(curl -sG "${GRAPH}/oauth/access_token" \
  --data-urlencode "grant_type=fb_exchange_token" \
  --data-urlencode "client_id=${FB_APP_ID}" \
  --data-urlencode "client_secret=${FB_APP_SECRET}" \
  --data-urlencode "fb_exchange_token=${FB_SHORT_LIVED_USER_TOKEN}")

if echo "$LL_RESP" | jq -e '.error' >/dev/null; then
  echo "$LL_RESP" | jq .
  die "user-token exchange failed"
fi
LL_USER_TOKEN=$(echo "$LL_RESP" | jq -r .access_token)
ok "long-lived user token: ${LL_USER_TOKEN:0:20}…"

# ---------------------------------------------------------------------------
log "Step 2/4 — Long-lived user token → never-expire Page token"
PG_RESP=$(curl -sG "${GRAPH}/${FB_PAGE_ID}" \
  --data-urlencode "fields=access_token,name" \
  --data-urlencode "access_token=${LL_USER_TOKEN}")

if echo "$PG_RESP" | jq -e '.error' >/dev/null; then
  echo "$PG_RESP" | jq .
  die "page-token exchange failed"
fi
FB_PAGE_TOKEN=$(echo "$PG_RESP" | jq -r .access_token)
PG_NAME=$(echo "$PG_RESP" | jq -r .name)
ok "page token for '${PG_NAME}': ${FB_PAGE_TOKEN:0:20}…"

# ---------------------------------------------------------------------------
log "Step 3/4 — Verify expiry (must be 0 = never)"
DBG=$(curl -sG "${GRAPH}/debug_token" \
  --data-urlencode "input_token=${FB_PAGE_TOKEN}" \
  --data-urlencode "access_token=${FB_APP_ID}|${FB_APP_SECRET}")

EXPIRES=$(echo "$DBG" | jq -r '.data.expires_at // empty')
TYPE=$(echo "$DBG" | jq -r '.data.type // empty')
SCOPES=$(echo "$DBG" | jq -r '.data.scopes | join(",")')

if [ "$EXPIRES" != "0" ]; then
  echo "$DBG" | jq .
  die "page token still has expiry (${EXPIRES}). Did you skip the long-lived user-token step?"
fi
ok "type=${TYPE} expires=never scopes=${SCOPES}"

# ---------------------------------------------------------------------------
log "Step 4/4 — Subscribe page to webhook fields (${SUBSCRIBE_FIELDS})"
SUB=$(curl -sX POST "${GRAPH}/${FB_PAGE_ID}/subscribed_apps" \
  -d "subscribed_fields=${SUBSCRIBE_FIELDS}" \
  -d "access_token=${FB_PAGE_TOKEN}")
if echo "$SUB" | jq -e '.error' >/dev/null; then
  echo "$SUB" | jq .
  die "subscribed_apps call failed"
fi
ok "page subscribed: $(echo "$SUB" | jq -c .)"

# ---------------------------------------------------------------------------
log "Writing values into ${ENV_FILE}"
[ -f "${ENV_FILE}" ] || { cp "${ENV_FILE}.example" "${ENV_FILE}" 2>/dev/null \
  || die "${ENV_FILE} not found and no .env.example to copy"; }

set_kv() {
  local key="$1"; local val="$2"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "${ENV_FILE}"
  else
    printf '\n%s=%s\n' "${key}" "${val}" >> "${ENV_FILE}"
  fi
}
set_kv FB_APP_ID         "${FB_APP_ID}"
set_kv FB_APP_SECRET     "${FB_APP_SECRET}"
set_kv FB_PAGE_ID        "${FB_PAGE_ID}"
set_kv FB_PAGE_TOKEN     "${FB_PAGE_TOKEN}"
set_kv FB_VERIFY_TOKEN   "${FB_VERIFY_TOKEN}"
chmod 600 "${ENV_FILE}"
ok "env updated"

if systemctl list-unit-files | grep -q '^fb-webhook.service'; then
  log "Restarting fb-webhook"
  systemctl restart fb-webhook
  sleep 2
  systemctl is-active fb-webhook && ok "fb-webhook running"
fi

# ---------------------------------------------------------------------------
cat <<MSG

============================================================
  All done.

  Save these for the Meta dashboard:
      FB_VERIFY_TOKEN = ${FB_VERIFY_TOKEN}
      FB_PAGE_TOKEN   = ${FB_PAGE_TOKEN}

  Then in https://developers.facebook.com/ → your App → Webhooks:
    1. Add a Page subscription
    2. Callback URL : https://api.<your-domain>/webhook/messenger
    3. Verify Token : the value above
    4. Subscribe fields: ${SUBSCRIBE_FIELDS}
    5. Click "Test" — you should see the request in
       /var/log/fb-webhook.log within seconds.
============================================================
MSG
