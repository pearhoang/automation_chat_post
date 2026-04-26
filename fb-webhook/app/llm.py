"""LLM call used to draft a reply to a customer message or comment.

Currently uses OpenAI Chat Completions. Swap out with Anthropic / Gemini /
local model by editing this single file.
"""

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Bạn là nhân viên CSKH của Demo Shop, một cửa hàng bán điện thoại Apple cũ "
    "và phụ kiện. Trả lời ngắn gọn, lịch sự, đúng trọng tâm tiếng Việt. "
    "Không tự bịa giá hay ưu đãi. Nếu khách hỏi giá cụ thể, mời khách đến cửa "
    "hàng hoặc để lại số điện thoại để được tư vấn chính xác. Cuối tin nhắn "
    "luôn chèn '— Demo Shop'."
)


async def draft_reply(user_text: str, *, context: str | None = None) -> str:
    if not settings.openai_api_key:
        return ""  # auto-reply disabled when no key

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "system", "content": f"Context: {context}"})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": settings.openai_model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 220,
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        if r.status_code >= 400:
            log.error("LLM error %s: %s", r.status_code, r.text)
            return ""
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
