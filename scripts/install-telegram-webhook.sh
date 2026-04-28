#!/usr/bin/env bash
# Register the Telegram bot's webhook URL with Telegram Bot API.
#
# Run on the VPS (or anywhere with Internet) AFTER you have set
# TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET in /opt/fb-webhook/.env.
#
# Usage:
#   sudo ./install-telegram-webhook.sh                                         # uses /opt/fb-webhook/.env
#   ./install-telegram-webhook.sh --env /custom/path/.env --url https://x/y    # explicit
#   ./install-telegram-webhook.sh --delete                                     # remove webhook
#
# What it does:
#   1. POST setWebhook URL=https://api.jazzrelaxation.com/webhook/telegram
#      with secret_token from .env
#   2. Restrict allowed_updates to only "message" + "edited_message"
#   3. Print getWebhookInfo to confirm

set -euo pipefail

ENV_FILE="/opt/fb-webhook/.env"
PUBLIC_URL="https://api.jazzrelaxation.com/webhook/telegram"
ACTION="set"

while [ $# -gt 0 ]; do
  case "$1" in
    --env)    ENV_FILE="$2"; shift 2 ;;
    --url)    PUBLIC_URL="$2"; shift 2 ;;
    --delete) ACTION="delete"; shift ;;
    *)        echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "TELEGRAM_BOT_TOKEN missing in $ENV_FILE" >&2
  exit 1
fi
TG="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

if [ "$ACTION" = "delete" ]; then
  echo "→ deleteWebhook"
  curl -fsS -X POST "${TG}/deleteWebhook" -d 'drop_pending_updates=true' | jq .
  exit 0
fi

if [ -z "${TELEGRAM_WEBHOOK_SECRET:-}" ]; then
  echo "TELEGRAM_WEBHOOK_SECRET missing — generate with: openssl rand -hex 32" >&2
  exit 1
fi

echo "→ setWebhook url=${PUBLIC_URL}"
curl -fsS -X POST "${TG}/setWebhook" \
  -d "url=${PUBLIC_URL}" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","edited_message"]' \
  -d 'drop_pending_updates=true' \
  | jq .

echo
echo "→ getWebhookInfo"
curl -fsS "${TG}/getWebhookInfo" | jq .

echo
echo "→ getMe"
curl -fsS "${TG}/getMe" | jq '.result | {id, username, first_name}'
