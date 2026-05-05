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
│   │   ├── inventory.py       #   product catalog lookup for grounded replies
│   │   ├── conversation.py    #   lightweight conversation context
│   │   ├── telegram.py        #   Telegram admin bot / notifications
│   │   ├── shop_info.py       #   mutable shop policy/profile context
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

### 3a. The two parts that have to be done by hand (FB has no API for them)

Meta does **not** expose a public API for creating Facebook Apps or for
generating the initial user token, and trying to script it with browser
automation is exactly the kind of activity that gets the source FB account
flagged. So:

1. **Create a Facebook App** at <https://developers.facebook.com/>:
   - "Create App" → choose **Business** type → name it.
   - Settings → Basic → write down **App ID** and **App Secret**.
   - Add the **Messenger** product, add the **Webhooks** product.
   - Under Messenger → "Access Tokens", connect your Page → write down
     **Page ID**.
2. **Generate a short-lived user token** in
   <https://developers.facebook.com/tools/explorer/>:
   - Pick your app from the dropdown.
   - "Get Token" → "Get User Access Token".
   - Tick scopes: `pages_manage_posts`, `pages_messaging`,
     `pages_read_engagement`, `pages_manage_engagement`,
     `pages_show_list`, `pages_manage_metadata`.
   - Click **Generate Access Token** and copy the long string starting
     with `EAA…`. This expires in ~1 hour, so do step 3b right after.

That's it. ~5 minutes.

### 3b. Let the helper script do the rest

The script `scripts/fb-token-exchange.sh` automates **all the remaining
steps**:

- short-lived → long-lived user token (60 d)
- long-lived user token → **never-expire** page token
- verify with `debug_token` (`expires_at == 0`)
- subscribe the Page to webhook fields (`messages`, `messaging_postbacks`,
  `feed`, `messaging_referrals`, `message_deliveries`)
- write `FB_APP_ID`, `FB_APP_SECRET`, `FB_PAGE_ID`, `FB_PAGE_TOKEN`,
  `FB_VERIFY_TOKEN` into `/opt/fb-webhook/.env`
- restart `fb-webhook`

```bash
ssh root@<vps>
FB_APP_ID=1234567890 \
FB_APP_SECRET=abcdef0123456789 \
FB_PAGE_ID=987654321 \
FB_SHORT_LIVED_USER_TOKEN=EAAB... \
bash /tmp/fb-token-exchange.sh
```

Or run it without env vars and it will prompt for each value.

A random `FB_VERIFY_TOKEN` is generated for you — the script prints it at
the end so you can paste it into the Meta dashboard.

After this you only need to add `OPENAI_API_KEY` / `OPENAI_MODEL` to
`.env`:

```bash
sudo -e /opt/fb-webhook/.env       # add OPENAI_API_KEY=sk-…
sudo systemctl restart fb-webhook
```

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

## 4b. FastAPI Telegram admin bot (đường ngắn, không qua OpenClaw)

Thay cho cách OpenClaw ở `§4`, repo có sẵn 1 admin bot tích hợp thẳng vào
`fb-webhook` (cùng process, cùng domain) — nhanh nhất nếu bạn chỉ cần đăng
bài lên Page từ Telegram.

### 4b.1 Tạo bot

1. Mở https://t.me/BotFather → `/newbot` → đặt tên → lấy **token**
2. Sinh 1 secret để xác thực webhook:
   ```bash
   openssl rand -hex 32
   ```
3. Gắn vào `/opt/fb-webhook/.env`:
   ```bash
   sed -i \
     -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=<token-từ-BotFather>|" \
     -e "s|^TELEGRAM_ADMIN_CHAT_ID=.*|TELEGRAM_ADMIN_CHAT_ID=|" \
     -e "s|^TELEGRAM_WEBHOOK_SECRET=.*|TELEGRAM_WEBHOOK_SECRET=<chuỗi-vừa-sinh>|" \
     /opt/fb-webhook/.env
   systemctl restart fb-webhook
   ```

### 4b.2 Đăng ký webhook với Telegram

```bash
sudo /usr/local/bin/install-telegram-webhook.sh
```
(Script gọi `setWebhook` trỏ về `https://api.jazzrelaxation.com/webhook/telegram`
kèm `secret_token`.)

### 4b.3 Lấy chat_id của bạn → whitelist

Mở chat với bot vừa tạo trên Telegram → gõ `/whoami` → bot trả lại 1 dãy số.
Set vào `.env` rồi restart:

```bash
sed -i "s|^TELEGRAM_ADMIN_CHAT_ID=.*|TELEGRAM_ADMIN_CHAT_ID=<chat_id>|" \
  /opt/fb-webhook/.env
systemctl restart fb-webhook
```

### 4b.4 Lệnh có sẵn

| Lệnh | Tác dụng |
|---|---|
| `/post <nội dung>` | Đăng bài text lên Page |
| `/post_link <url> \| <caption>` | Đăng bài kèm link |
| `/post_img <caption>` (gửi kèm 1 ảnh) | Đăng bài có ảnh |
| `/status` | Health check + thông tin Page Token |
| `/whoami` | Trả về `chat_id` (dùng để whitelist) |
| `/help` | Liệt kê lệnh |

Bot trả về URL bài viết sau khi đăng dạng `https://www.facebook.com/<post_id>`.

### 4b.5 Bảo mật

* `secret_token` chặn POST giả mạo (FastAPI trả 401 nếu sai header)
* Whitelist `TELEGRAM_ADMIN_CHAT_ID` chặn user khác ra lệnh
* Dùng cùng cert TLS sẵn có ở `api.jazzrelaxation.com`

### 4b.6 Đổi token nếu lộ

```bash
# 1. BotFather → /revoke → chọn bot → token mới
sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=<token-mới>|" /opt/fb-webhook/.env
systemctl restart fb-webhook
sudo /usr/local/bin/install-telegram-webhook.sh
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

## 6. Common pitfalls (real issues we hit during the first install)

> Read this before you re-run the installer on a new machine. Every entry here
> is something we had to debug or work around live during the first
> provisioning of `109.123.233.131`.

### 6.1 Cloudflare proxy will break Caddy ACME

If your DNS is on Cloudflare (or any CDN), set the records to **DNS only**
(grey cloud), **not** "Proxied" (orange cloud).

- HTTP-01 ACME challenge requires Let's Encrypt to reach **your** VPS IP on
  port 80. With Cloudflare proxying, it reaches Cloudflare instead and gets
  Cloudflare's edge cert.
- After issuing certs, you may flip back to "Proxied" if you want the CDN —
  but then switch Caddy to use **DNS-01** challenge with Cloudflare API token,
  otherwise the renewal in 60 days will fail.

```bash
# Quick check: must show your VPS IP, not a Cloudflare IP
getent hosts live.jazzrelaxation.com
```

### 6.2 Caddy gets stuck if DNS isn't ready when it first starts

If Caddy starts before the A record propagates, ACME fails with
`NXDOMAIN looking up A` and Caddy enters exponential backoff (next retry can
be 15–30 minutes away). Symptom: `curl: (35) ... tlsv1 alert internal error`.

Fix once DNS is correct:

```bash
systemctl restart caddy
# wait ~10 s, then:
curl -I https://live.jazzrelaxation.com/
```

A plain `systemctl reload caddy` is **not** enough — it does not reset the
backoff timer. Always **restart**.

### 6.3 Caddy log directory permission

The Debian package creates `/var/log/caddy` owned by root, but the service
runs as user `caddy`. Without fixing ownership, every reload fails with
`open /var/log/caddy/...: permission denied`.

```bash
chown -R caddy:caddy /var/log/caddy
chmod 755 /var/log/caddy
```

The installer script does this automatically; if you change the log path in
the Caddyfile, redo the chown.

### 6.4 `EnvironmentFile` does not strip inline comments

systemd's `EnvironmentFile=` parser treats `#` only at the **start** of a
line. A line like:

```env
AUTO_REPLY_ENABLED=false   # safety: dry-run
```

is loaded as the literal string `false   # safety: dry-run`, and pydantic
rejects it with `ValidationError: Input should be a valid boolean`.

Always put comments on their own line:

```env
# Safety: dry-run
AUTO_REPLY_ENABLED=false
```

The shipped `.env.example` follows this rule.

### 6.5 Caddy reload hangs while it's obtaining a new cert

If you change the Caddyfile to add a new domain and run
`systemctl reload caddy`, the reload command can block until ACME completes
or systemd's reload timeout fires. Symptom: shell sits there for 90+ s.

Use `caddy validate --config /etc/caddy/Caddyfile` to syntax-check first,
then `systemctl restart caddy` — it returns immediately and ACME runs in the
background.

### 6.6 ClawHub rate limit (HTTP 429) when installing the skill

```
$ openclaw skills install facebook-page
ClawHub /api/v1/download failed (429): Rate limit exceeded
```

Workaround used in the installer: `git clone` the skill straight into the
workspace:

```bash
git clone https://github.com/tinbeta/facebook-page-skill.git \
  /root/.openclaw/workspace/skills/facebook-page
```

OpenClaw scans the workspace `skills/` directory at startup and treats it
identically to a ClawHub-installed skill. `openclaw skills list` should now
show `📦facebook  ready  openclaw-workspace`.

### 6.7 OpenClaw `onboard` is interactive by default

If you forget the `--non-interactive` flag, `openclaw onboard` blocks
forever waiting for keyboard input — and SSH+systemd will time out the
command. The non-interactive incantation we use:

```bash
openclaw onboard \
  --non-interactive --accept-risk \
  --install-daemon --flow quickstart \
  --auth-choice skip \
  --gateway-bind loopback \
  --gateway-auth token
```

`--auth-choice skip` lets you postpone picking an LLM provider. Add it later
with `openclaw configure --section providers`.

### 6.8 OpenClaw runs as a `systemd --user` unit, not a system unit

The daemon installs to `/root/.config/systemd/user/openclaw-gateway.service`
and is enabled via `loginctl enable-linger root` (the installer does this
silently). Status check:

```bash
# Wrong: nothing shown
systemctl status openclaw-gateway

# Right:
systemctl --user --machine=root@.host status openclaw-gateway
journalctl --user --user-unit openclaw-gateway -f
```

### 6.9 Postgres "could not change directory to /root" warnings

```
$ sudo -u postgres psql -c "..."
could not change directory to "/root": Permission denied
```

Cosmetic only — postgres cannot `cd` into root's home. The query still runs.
Either `cd /tmp` first or ignore the warning.

### 6.10 Contabo images ship with cups + xrdp listening

Default Contabo Ubuntu 22.04 has CUPS on `:631` and xrdp on `:3389/3350`.
You don't need them and ufw blocks external access, but they're worth
disabling:

```bash
systemctl disable --now xrdp xrdp-sesman 2>/dev/null
systemctl mask cups cups-browsed 2>/dev/null
```

The installer does this.

### 6.11 Webhook signature verification

Meta signs every webhook POST with `X-Hub-Signature-256` using your
**App Secret**. The handler refuses requests with a bad signature.

If you see `Invalid signature, dropping event` in `/var/log/fb-webhook.log`
during testing, double-check that:

1. `FB_APP_SECRET` in `.env` matches the App Secret in
   <https://developers.facebook.com/> → Settings → Basic.
2. You restarted `fb-webhook` after editing `.env`.
3. The "Test" button in the Meta UI uses the **App** signature; if you're
   using `curl` to send fake events from a script, you have to sign them with
   the same secret or temporarily comment out the signature check.

### 6.12 The 24-hour Messenger window

The default `fb_client.send_message()` uses
`messaging_type=RESPONSE`. That works only for the first 24 h after the
user's last inbound message. After that, Meta returns
`(#10) This message is sent outside of allowed window`.

For broadcasts, customer-care follow-ups, etc., switch to
`messaging_type=MESSAGE_TAG` with a valid tag (`HUMAN_AGENT`,
`CONFIRMED_EVENT_UPDATE`, `POST_PURCHASE_UPDATE`). See the comment in
`fb-webhook/app/fb_client.py`.

### 6.13 Page Token expires anyway?

If your Graph API call returns `(#190) Error validating access token`:

- You probably saved the **short-lived user token** by accident. Re-run the
  3-step exchange in `~/.openclaw/workspace/skills/facebook-page/SKILL.md`.
- The Page Token only becomes never-expiring if you exchange it from the
  **long-lived user token** (60-day) — not directly from the short-lived
  one.
- Verify with:
  ```bash
  curl "https://graph.facebook.com/v21.0/debug_token?input_token=$FB_PAGE_TOKEN&access_token=$FB_APP_ID|$FB_APP_SECRET"
  ```
  Look for `"expires_at": 0` in the response.

### 6.13b Short-lived token missing scopes → `(#200) ... permissions`

If `fb-token-exchange.sh` succeeds at the page-token step but Step 4
(subscribe webhook) fails with:

```
(#200) To subscribe to the messages field, one of these permissions
is needed: pages_messaging.
To subscribe to the feed field, one of these permissions is needed:
pages_manage_metadata.
```

…the short-lived **user** token you generated in Graph API Explorer did
not have the right scopes ticked. The Page Token derived from it
inherits the same scope set, so re-running Step 4 won't help.

Fix:

1. Open <https://developers.facebook.com/tools/explorer/>.
2. Pick your app, click **Add a Permission**, and tick **all** of:
   `pages_manage_posts`, `pages_messaging`, `pages_read_engagement`,
   `pages_manage_engagement`, `pages_show_list`, `pages_manage_metadata`.
3. Click **Generate Access Token** again — you must regenerate;
   ticking a scope on an already-issued token does not retroactively
   add it.
4. Re-run `fb-token-exchange.sh` with the new token. The script's
   Step 0 will up-front diff the granted scopes against the required
   set and abort early if anything is missing, so you don't waste a
   short-lived token on a doomed exchange.

### 6.14 Comments not arriving

Two separate subscriptions are needed in the Meta dashboard:

1. **Webhook → Page object**, fields `messages` + `messaging_postbacks` →
   delivers Messenger inbox.
2. **Webhook → Page object**, field `feed` → delivers post comments.

Most people add `messages` and forget `feed`. Comments will silently never
arrive. Re-check at
<https://developers.facebook.com/> → App → Webhooks → Page.

### 6.15 `fb-webhook` boots but `/healthz` returns 502 from `api....`

That means Caddy reached your VPS but uvicorn isn't on `127.0.0.1:8000`.
Check:

```bash
ss -tlnp | grep 8000          # uvicorn must be listening
journalctl -u fb-webhook -n 50 # boot errors (usually .env)
```

### 6.16 NodeSource setup script may fail on non-default architectures

The `setup_24.x` script supports x86_64 and arm64 only. On exotic VPSes
(s390x, riscv) you'll need to install Node from <https://nodejs.org/dist>
manually. Check with `dpkg --print-architecture`.

### 6.17 OpenClaw + glibc

OpenClaw's prebuilt binaries assume glibc ≥ 2.31. Ubuntu 22.04 ships
2.35 → fine. Older distros (Ubuntu 18.04, Debian 10) will need either an
upgrade or building OpenClaw from source.

### 6.18 fail2ban whitelist your own IP

If you SSH a lot from your home IP, add yourself to fail2ban's whitelist
before the first ban:

```bash
echo '[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 <YOUR_HOME_IP>/32' \
  > /etc/fail2ban/jail.d/00-whitelist.conf
systemctl restart fail2ban
```

### 6.19 Quick all-services smoke test

Run after install completes:

```bash
for s in caddy postgresql redis-server fb-webhook fail2ban; do
  printf "%-20s %s\n" "$s" "$(systemctl is-active $s)"
done
systemctl --user --machine=root@.host is-active openclaw-gateway

curl -sI https://live.jazzrelaxation.com/ | head -1
curl  -s https://api.jazzrelaxation.com/healthz
```

Expected:

```
caddy             active
postgresql        active
redis-server      active
fb-webhook        active
fail2ban          active
openclaw-gateway  active

HTTP/2 200
{"status":"ok","auto_reply":false}
```

---

## 7. Security checklist before going to production

- [ ] Disable root password auth: `passwd -l root`, allow only the SSH key
- [ ] Rotate the temporary root password used during initial setup. The deploy
  key in `/root/.ssh/authorized_keys` is the only thing we need afterwards.
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
