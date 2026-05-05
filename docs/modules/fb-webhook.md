# Module: fb-webhook

## Responsibility

FastAPI app xử lý:

- Messenger webhook nhận tin nhắn và gửi reply qua Graph API.
- Page feed webhook nhận comment, classify, auto reply/hide/delete/forward.
- Telegram admin bot cho posting/control và thông báo lead nóng.
- Inventory lookup để LLM không bịa tình trạng/giá máy.

## Entry Points

- `fb-webhook/app/main.py`: FastAPI routes và event orchestration.
- `fb-webhook/app/llm.py`: prompt, reply drafting, comment classification.
- `fb-webhook/app/fb_client.py`: Graph API calls.
- `fb-webhook/app/telegram.py`: Telegram send/admin helpers.
- `fb-webhook/app/inventory.py`: inventory catalog/search.
- `fb-webhook/app/conversation.py`: lightweight conversation state.
- `fb-webhook/app/shop_info.py`: shop policy/profile context cho LLM.
- `fb-webhook/app/config.py`: `.env` settings.

## Pitfalls

- `HUMAN_REVIEW_QUEUE=true` chỉ log draft/review, không gửi thẳng.
- Auto-hide comment không xóa hoàn toàn với người comment và bạn bè của họ; auto-delete dùng endpoint khác và không reversible.
- Không sync `fb-webhook/conversations/*.json` vào Git.
- Khi thêm setting mới trong `config.py`, cập nhật `fb-webhook/.env.example`.
