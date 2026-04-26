# automation_chat_post

Single-VPS stack to **(1)** host a static Apple-style storefront ("Demo Shop")
and **(2)** run a Facebook Page automation pipeline:

* **Caddy** — auto-TLS reverse proxy + static file server for the site
* **OpenClaw** (Node 24) — agent gateway with `facebook-page` skill, you talk to
  it from Telegram / WhatsApp / etc. and it posts to your FB Page on demand or
  on a cron schedule
* **FastAPI webhook** (Python 3.11) — receives Messenger inbox events + page
  comment events from Meta, drafts replies via an LLM, optionally sends them
  back through the Page Send API
* **Postgres + Redis** — durable state and a queue for review / retry
* **ufw + fail2ban** — basic hardening

> Stack chosen after evaluating browser-automation alternatives. See
> [`docs/STACK_DECISION.md`](docs/STACK_DECISION.md) for the full reasoning
> (API rate limits, FB ban risk vs anti-detect browsers, OpenClaw vs
> hermes-agent, etc.).

---

## Repository layout

```
.
├── index.html                 # Demo Shop single-page site (Tailwind via CDN)
├── img/                       # Product mockups (Apple)
├── fb-webhook/                # FastAPI webhook server
│   ├── app/
│   │   ├── main.py            #   GET /healthz, /webhook/messenger handlers
│   │   ├── fb_client.py       #   Graph API wrapper
│   │   ├── llm.py             #   OpenAI (or any) draft-reply call
│   │   └── config.py          #   pydantic-settings → reads .env
│   ├── requirements.txt
│   └── .env.example
├── systemd/
│   └── fb-webhook.service
├── scripts/
│   └── install.sh             # idempotent one-shot installer
├── docs/
│   └── STACK_DECISION.md
└── README.md
```

---

## 0. What you need before starting

| Item | Where to get it |
|---|---|
| **VPS** | Ubuntu 22.04 LTS, ≥ 2 GB RAM, ≥ 20 GB disk, public IP. Tested on Contabo. |
| **Domain + DNS** | Two A records pointing to the VPS IP: `live.<your-domain>` (site) and `api.<your-domain>` (webhook). Caddy obtains certs only after DNS resolves. |
| **Facebook App** | <https://developers.facebook.com/> → My Apps → Create App. Note **App ID** and **App Secret**. |
| **Facebook Page** | The Page you want to automate. Note its **Page ID** (Settings → About). |
| **Page Token (never-expire)** | Procedure inside the skill: `~/.openclaw/workspace/skills/facebook-page/SKILL.md`. Short summary: short-lived user token → long-lived user token (60 d) → exchange for page token (`expires_at: 0`). |
| **LLM API key** | OpenAI (default), Anthropic, or Gemini. |
| **(Optional) Telegram bot** | Talk to <https://t.me/BotFather>, get a bot token. Used as the OpenClaw control channel and the human-review queue. |

---

## 1. One-command install

SSH into the VPS as root and run:

```bash
curl -fsSL https://raw.githubusercontent.com/pearhoang/automation_chat_post/main/scripts/install.sh \
  | SITE_DOMAIN=live.jazzrelaxation.com \
    API_DOMAIN=api.jazzrelaxation.com \
    bash
```

The script is idempotent and will:

1. Update apt + install base tools (`curl`, `git`, `rsync`, `tmux`, …)
2. Configure **ufw** (only 22, 80, 443 open) + enable **fail2ban**
3. Install **Node 24** (NodeSource), **Python 3.11** (deadsnakes),
   **Postgres** + **Redis**, **Caddy**
4. Pull the site and publish to `/var/www/<SITE_DOMAIN>/`
5. Write `/etc/caddy/Caddyfile` with both vhosts (auto-TLS via Let's Encrypt)
6. `npm i -g openclaw`, run `openclaw onboard --non-interactive --install-daemon`,
   start the gateway as a systemd-user service
7. Clone `tinbeta/facebook-page-skill` into the OpenClaw workspace
8. Create the FastAPI venv at `/opt/fb-webhook/.venv`, install requirements,
   write `/etc/systemd/system/fb-webhook.service`, enable + start it
9. Print a status summary + the next steps

If you need to run it again, re-run the same command — every step checks
existing state.

---

## 2. Add DNS records

Once the VPS IP is known (printed at the end of `install.sh`), add at your DNS
provider:

```
live.jazzrelaxation.com    A    <VPS_IP>
api.jazzrelaxation.com     A    <VPS_IP>
```

Caddy is already configured. As soon as DNS propagates, the next request will
trigger ACME and a real Let's Encrypt cert is issued. No manual cert work.

Verify:

```bash
curl -I https://live.jazzrelaxation.com/
curl   https://api.jazzrelaxation.com/healthz
```

---

## 3. Facebook integration

### 3a. Create the FB App + get tokens

Follow the recipe in
`/root/.openclaw/workspace/skills/facebook-page/SKILL.md` (it is the
`tinbeta/facebook-page-skill` README). Required values:

| Variable | Value |
|---|---|
| `FB_APP_ID` | App ID from developer dashboard |
| `FB_APP_SECRET` | App Secret |
| `FB_PAGE_ID` | numeric Page ID |
| `FB_PAGE_TOKEN` | Never-expire page token (`expires_at: 0`) |
| `FB_VERIFY_TOKEN` | Any random string you choose; you will paste the same value into the FB App webhook UI |

### 3b. Fill in `.env`

```bash
sudo -e /opt/fb-webhook/.env       # paste real values
sudo systemctl restart fb-webhook
```

You can also set `OPENAI_API_KEY` / `OPENAI_MODEL` here.

### 3c. Subscribe the webhook in Meta dashboard

1. <https://developers.facebook.com/> → your App → **Webhooks**
2. **Object: Page** → Subscribe
3. **Callback URL:** `https://api.jazzrelaxation.com/webhook/messenger`
4. **Verify Token:** the value of `FB_VERIFY_TOKEN` in your `.env`
5. **Subscribe to fields:** `messages`, `messaging_postbacks`, `feed` (for
   comments), `messaging_referrals`, `message_deliveries`
6. Add the **Messenger** product to the App, link it to your Page, click
   "Generate Token" — the token here is the same `FB_PAGE_TOKEN` you put in
   the `.env`.

### 3d. (Optional) The 24-hour rule

After 24 h since the user's last message, the Send API will reject plain
`messaging_type=RESPONSE`. Switch to `MESSAGE_TAG` with a valid tag
(`HUMAN_AGENT`, `CONFIRMED_EVENT_UPDATE`, `POST_PURCHASE_UPDATE`) — see
`fb_client.py`. The default scaffold uses `RESPONSE` — adjust before going
live.

---

## 4. OpenClaw channels (control your Page from Telegram)

After install, OpenClaw is running on `127.0.0.1:18789` with a token-only
gateway. To attach a Telegram bot and use the `facebook-page` skill from your
phone:

```bash
# 1. Set your default LLM provider (OpenAI shown — see `openclaw configure --help` for others)
openclaw configure --section providers
# paste your OPENAI_API_KEY when asked

# 2. Add Telegram channel
openclaw channels add telegram
# paste your BotFather token when asked

# 3. Verify
openclaw status
openclaw skills list | grep facebook
```

Example chat from Telegram:

```
> Đăng bài lên Page với caption: "iPhone 13 Pro Max 256GB Sierra Blue, máy đẹp 99%, bảo hành 12T. Inbox để nhận giá tốt." kèm ảnh /opt/skills/facebook-page-skill/references/*.jpg
> Lên lịch đăng 19:00 hôm nay
> Reply bình luận mới nhất với link sản phẩm trên live.jazzrelaxation.com
```

Cron jobs:

```bash
openclaw cron add --name daily-promo --schedule "0 19 * * *" \
  --task "Tạo bài khuyến mãi iPhone hôm nay và đăng lên Page"
```

---

## 5. Day-2 ops

### Service status

```bash
systemctl status caddy fb-webhook postgresql redis-server fail2ban
systemctl --user --machine=root@.host status openclaw-gateway
```

### Logs

| File | Purpose |
|---|---|
| `/var/log/caddy/live.jazzrelaxation.com.log` | Site access/error |
| `/var/log/caddy/api.jazzrelaxation.com.log`  | Webhook reverse-proxy access |
| `/var/log/fb-webhook.log`                    | FastAPI app stdout/stderr |
| `journalctl -u caddy -n 200`                 | Caddy lifecycle / ACME |
| `journalctl --user -u openclaw-gateway -f`   | OpenClaw events (run as root) |

### Update site

Just push to the configured branch (`main` by default) and re-deploy:

```bash
ssh root@<vps>
cd /tmp && git clone --depth 1 https://github.com/pearhoang/automation_chat_post.git
rsync -a --delete --exclude='.git' --exclude='scripts' --exclude='fb-webhook' \
  --exclude='systemd' --exclude='docs' /tmp/automation_chat_post/ \
  /var/www/live.jazzrelaxation.com/
rm -rf /tmp/automation_chat_post
```

### Update webhook code

```bash
# pull latest .py files into /opt/fb-webhook/app/
systemctl restart fb-webhook
```

### Update OpenClaw

```bash
npm i -g openclaw@latest
systemctl --user --machine=root@.host restart openclaw-gateway
```

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `curl https://live....` → connection refused | DNS A record not yet pointing at VPS IP. Wait for propagation; verify with `dig +short live.jazzrelaxation.com`. |
| Caddy log shows ACME `unauthorized` | DNS resolves to a *different* IP (CDN / old record). Caddy uses HTTP-01 by default; the IP must be this VPS. |
| `fb-webhook` keeps restarting | Inspect `/var/log/fb-webhook.log`. Most common: `.env` has invalid value or missing token. Booleans must be plain `true` / `false`, no inline comments. |
| Webhook verification fails in FB UI | `FB_VERIFY_TOKEN` differs between `.env` and the Meta UI. They must match exactly. |
| Send API returns `(#10) Application does not have permission` | Page Token is short-lived or missing `pages_messaging` permission. Re-issue via Graph API Explorer. |
| Comments not arriving | Webhook is subscribed to **Page** object but not to the **feed** field. Re-check subscriptions. |
| OpenClaw rate-limited installing skills | ClawHub free tier = a few requests / minute. Either wait a minute or, as we did here, `git clone` the skill repo straight into `~/.openclaw/workspace/skills/`. |
| `openclaw skills list` shows the skill but `gog` says `needs setup` | That's the unrelated `gog` (Google Workspace) skill, harmless. |

---

## 7. Security checklist before going to production

- [ ] Disable root password auth: `passwd -l root`, allow only the SSH key
- [ ] Rotate `dDGK2b9YmW6012l0qCkjApBIVR` (the password used during initial
  setup). The deploy key in `/root/.ssh/authorized_keys` is the only thing we
  need afterwards.
- [ ] Set `FB_VERIFY_TOKEN` to `openssl rand -hex 32`.
- [ ] Lock postgres password: `sudo -u postgres psql -c "ALTER USER fbwebhook WITH PASSWORD '<strong>';"` and update `DATABASE_URL` in `.env`.
- [ ] Move `AUTO_REPLY_ENABLED=true` only after you have watched at least
  100 draft replies in `human_review_queue` mode.
- [ ] Take a Contabo snapshot before flipping auto-reply.

---

## 8. Why this stack (short answer)

* **API path, not browser automation.** Meta Graph API is the supported route
  for Pages — no anti-detect browser is needed and the ban risk is ~100×
  lower for a Page that pings Graph endpoints than one whose UI a headless
  Chrome is clicking.
* **OpenClaw** gives the agent loop, channels, cron, sandbox, and the skill
  marketplace for free.
* **`facebook-page-skill` (tinbeta)** is plug-and-play and already documents
  the never-expire token flow in Vietnamese.
* **FastAPI webhook** is the only piece that has to be custom — OpenClaw does
  not natively handle Messenger inbox webhooks, and we want a thin, fast
  endpoint that returns `200 OK` to Meta within 20 s and pushes the work into
  Redis.

Long version: [`docs/STACK_DECISION.md`](docs/STACK_DECISION.md).

---

## 9. License

The site code uses Apple product mockups for visual reference only. The
automation code is MIT-licensed.
