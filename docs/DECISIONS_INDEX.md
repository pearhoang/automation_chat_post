# Decisions Index

## Active Decisions

- `D-001` — Path chính dùng Meta Graph API + webhook cho Facebook Page, không dùng browser automation làm core.
- `D-002` — OpenClaw giữ vai trò agent/control plane và skill execution; FastAPI giữ vai trò webhook path nhanh, dễ kiểm soát.
- `D-003` — Không commit secret/runtime data; VPS code phải được sync về repo qua source files sạch.

## Where To Read More

- `docs/STACK_DECISION.md` chứa phân tích chi tiết ban đầu.
