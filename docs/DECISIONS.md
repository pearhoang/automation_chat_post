# Decisions

## D-001 — Use Meta Graph API/Webhooks As The Main Facebook Page Path

- Status: active
- Context: Page posting, Messenger replies, and comment handling are supported by official APIs. Browser automation raises UI fragility and account-risk concerns.
- Decision: Use Graph API + webhook for the production path. Keep browser automation only for rare platforms/tasks without APIs.

## D-002 — Split Agent Control From Webhook Serving

- Status: active
- Context: Meta webhooks need fast, predictable `200 OK` handling; agent loops can be slower and more variable.
- Decision: FastAPI handles webhook ingress and deterministic dispatch. OpenClaw handles admin/control-plane tasks and skills.

## D-003 — Treat VPS Runtime As Deployment State, Not Source Of Truth

- Status: active
- Context: Some fixes were made directly on the VPS during live setup.
- Decision: Sync only cleaned source/config template changes back to Git. Exclude `.env`, tokens, logs, and conversation/runtime JSON.
