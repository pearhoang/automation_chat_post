#!/usr/bin/env bash
# =============================================================================
# install.sh — End-to-end provisioning for the Demo Shop / FB automation stack
#
# Stack: Caddy + Node 24 + OpenClaw + facebook-page-skill + FastAPI webhook
#        Postgres + Redis + ufw + fail2ban
#
# Tested on Ubuntu 22.04 LTS (Jammy). Run as root on a fresh VPS.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pearhoang/automation_chat_post/main/scripts/install.sh | bash
#   # or
#   bash install.sh
#
# Idempotent: re-running is safe.
# =============================================================================
set -euo pipefail

SITE_DOMAIN="${SITE_DOMAIN:-live.jazzrelaxation.com}"
API_DOMAIN="${API_DOMAIN:-api.jazzrelaxation.com}"
REPO_URL="${REPO_URL:-https://github.com/pearhoang/automation_chat_post.git}"
SITE_BRANCH="${SITE_BRANCH:-main}"
SKILL_REPO="${SKILL_REPO:-https://github.com/tinbeta/facebook-page-skill.git}"

log() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
}

step_apt() {
  log "Update apt + install base tools"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    curl wget git ufw fail2ban htop tmux unzip rsync ca-certificates gnupg \
    lsb-release software-properties-common build-essential debian-keyring \
    debian-archive-keyring apt-transport-https
}

step_firewall() {
  log "UFW + fail2ban"
  ufw --force reset >/dev/null
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 22/tcp comment ssh
  ufw allow 80/tcp comment http
  ufw allow 443/tcp comment https
  ufw --force enable
  systemctl enable --now fail2ban
}

step_node() {
  log "Install Node.js 24 (NodeSource)"
  if ! command -v node >/dev/null || [ "$(node -v | cut -dv -f2 | cut -d. -f1)" -lt 24 ]; then
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
    apt-get install -y -qq nodejs
  fi
  node -v
  npm -v
}

step_python() {
  log "Install Python 3.11"
  if ! command -v python3.11 >/dev/null; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev python3-pip
  fi
  python3.11 --version
}

step_db() {
  log "Postgres + Redis"
  apt-get install -y -qq postgresql postgresql-contrib redis-server
  systemctl enable --now postgresql redis-server
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='fbwebhook'" \
    | grep -q 1 \
    || sudo -u postgres psql -c "CREATE USER fbwebhook WITH PASSWORD 'fbwebhook';"
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='fbwebhook'" \
    | grep -q 1 \
    || sudo -u postgres createdb -O fbwebhook fbwebhook
  sudo -u postgres psql -c \
    "GRANT ALL PRIVILEGES ON DATABASE fbwebhook TO fbwebhook;" >/dev/null
}

step_caddy() {
  log "Install Caddy + write Caddyfile"
  if ! command -v caddy >/dev/null; then
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
      | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy
  fi

  mkdir -p /var/log/caddy
  chown -R caddy:caddy /var/log/caddy

  cat > /etc/caddy/Caddyfile <<EOF
${SITE_DOMAIN} {
    root * /var/www/${SITE_DOMAIN}
    encode zstd gzip
    file_server
    @cache path *.jpg *.jpeg *.png *.webp *.gif *.svg *.ico *.css *.js *.woff *.woff2
    header @cache Cache-Control "public, max-age=2592000, immutable"
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }
    log {
        output file /var/log/caddy/${SITE_DOMAIN}.log {
            roll_size 10MiB
            roll_keep 5
        }
    }
}

${API_DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        -Server
    }
    log {
        output file /var/log/caddy/${API_DOMAIN}.log {
            roll_size 10MiB
            roll_keep 5
        }
    }
}
EOF
  caddy validate --config /etc/caddy/Caddyfile
  systemctl restart caddy
}

step_site() {
  log "Pull site assets and publish to /var/www/${SITE_DOMAIN}"
  TMPSITE=$(mktemp -d)
  git clone --depth 1 --branch "${SITE_BRANCH}" "${REPO_URL}" "${TMPSITE}"
  mkdir -p "/var/www/${SITE_DOMAIN}"
  rsync -a --delete \
    --exclude='.git' --exclude='.github' --exclude='scripts' \
    --exclude='fb-webhook' --exclude='systemd' \
    "${TMPSITE}/" "/var/www/${SITE_DOMAIN}/"
  rm -rf "${TMPSITE}"
}

step_openclaw() {
  log "Install OpenClaw + onboard (loopback gateway, no LLM key yet)"
  if ! command -v openclaw >/dev/null; then
    npm install -g openclaw
  fi
  openclaw --version

  if ! systemctl --user --machine=root@.host is-active openclaw-gateway \
       >/dev/null 2>&1; then
    openclaw onboard --non-interactive --accept-risk \
      --install-daemon --flow quickstart --auth-choice skip \
      --gateway-bind loopback --gateway-auth token
  fi

  log "Install facebook-page-skill into workspace"
  mkdir -p /opt/skills /root/.openclaw/workspace/skills
  if [ ! -d /opt/skills/facebook-page-skill ]; then
    git clone "${SKILL_REPO}" /opt/skills/facebook-page-skill
  fi
  rm -rf /root/.openclaw/workspace/skills/facebook-page
  cp -r /opt/skills/facebook-page-skill /root/.openclaw/workspace/skills/facebook-page
}

step_webhook() {
  log "Set up FastAPI webhook at /opt/fb-webhook"
  mkdir -p /opt/fb-webhook
  if [ ! -d /opt/fb-webhook/.venv ]; then
    python3.11 -m venv /opt/fb-webhook/.venv
  fi
  /opt/fb-webhook/.venv/bin/pip install --quiet --upgrade pip wheel
  /opt/fb-webhook/.venv/bin/pip install --quiet -r /opt/fb-webhook/requirements.txt

  [ -f /opt/fb-webhook/.env ] || cp /opt/fb-webhook/.env.example /opt/fb-webhook/.env
  chmod 600 /opt/fb-webhook/.env

  touch /var/log/fb-webhook.log
  cp /opt/fb-webhook/systemd/fb-webhook.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now fb-webhook
}

step_summary() {
  log "Service status"
  systemctl --no-pager is-active caddy postgresql redis-server fb-webhook fail2ban || true
  systemctl --user --machine=root@.host is-active openclaw-gateway || true
  echo
  echo "Site host header check:"
  curl -sI -H "Host: ${SITE_DOMAIN}" http://127.0.0.1/ | head -5 || true
  echo
  echo "API health check:"
  curl -s http://127.0.0.1:8000/healthz || true
  echo
  echo
  cat <<MSG
============================================================
  Provisioning done.

  Next steps:
    1) Add DNS A records:
         ${SITE_DOMAIN}  ->  $(curl -s ifconfig.me || echo "this VPS IP")
         ${API_DOMAIN}   ->  $(curl -s ifconfig.me || echo "this VPS IP")
       Caddy will then auto-issue Let's Encrypt certificates.

    2) Fill in /opt/fb-webhook/.env with real Facebook + LLM credentials,
       then 'systemctl restart fb-webhook'.

    3) Configure OpenClaw channels (Telegram bot etc.):
         openclaw configure
         openclaw channels login

    4) Subscribe webhook in https://developers.facebook.com:
         Callback URL : https://${API_DOMAIN}/webhook/messenger
         Verify Token : value of FB_VERIFY_TOKEN in .env
         Subscribe to fields: messages, messaging_postbacks, feed

  Logs:
    /var/log/caddy/*.log
    /var/log/fb-webhook.log
    journalctl --user -u openclaw-gateway -f   (run as root)
============================================================
MSG
}

# ---------------------------------------------------------------------------
require_root
step_apt
step_firewall
step_node
step_python
step_db
step_caddy
step_site
step_openclaw
step_webhook
step_summary
