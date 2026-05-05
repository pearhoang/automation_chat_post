# Project Brief

## Purpose

`automation_chat_post` triển khai stack single-VPS cho Demo Shop:

- Host static storefront tại `live.jazzrelaxation.com`.
- Nhận Messenger/comment webhook tại `api.jazzrelaxation.com`.
- Dùng LLM để soạn/trả lời inbox, comment và chuyển lead nóng về Telegram admin.
- Dùng OpenClaw skills để đăng, sửa, xóa bài Facebook Page và quản lý inventory.

## Architecture

- `index.html`, `img/`: static storefront HTML + Tailwind CDN.
- `fb-webhook/`: FastAPI app chạy bằng uvicorn sau Caddy.
- `openclaw-skills/`: skills copy vào OpenClaw workspace trên VPS.
- `scripts/`: installer/token exchange/webhook helper scripts.
- `systemd/`: service unit cho `/opt/fb-webhook`.
- `docs/STACK_DECISION.md`: lý do chọn API/OpenClaw thay vì browser automation làm path chính.

## Runtime

- VPS Ubuntu, Caddy reverse proxy, Postgres, Redis.
- FastAPI service đọc `/opt/fb-webhook/.env`.
- Facebook Page webhook dùng `GET/POST /webhook/messenger`.
- Telegram admin bot dùng `POST /webhook/telegram`.
- LLM provider dùng OpenAI-compatible Chat Completions qua `OPENAI_BASE_URL`.

## Build And Test

```bash
cd fb-webhook
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m compileall app
```

Trên VPS:

```bash
systemctl restart fb-webhook
curl https://api.jazzrelaxation.com/healthz
tail -f /var/log/fb-webhook.log
```

## Invariants

- Không commit `.env`, token, password, log, hoặc dữ liệu conversation thật.
- Webhook phải trả `200 OK` nhanh cho Meta/Telegram; xử lý nặng nên nằm sau bước parse/validate.
- Multi-page SaaS chưa hoàn thiện; bản hiện tại vẫn thiên về single Page config qua `.env`.
- Khi đồng bộ từ VPS về repo, chỉ lấy source code/config mẫu, không lấy runtime state.
