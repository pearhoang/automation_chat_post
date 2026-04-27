"""LLM calls used to draft replies and classify customer comments.

Uses OpenAI-compatible Chat Completions (works with OpenAI, DeepSeek,
9Router, OpenRouter, …). Swap out by editing this single file.
"""

import json
import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Bạn đang trả lời khách của một shop bán iPhone / iPad / Mac cũ ở Hà Nội. "
    "Văn phong: như chính chủ shop nhắn tay — thân thiện, ngắn gọn, tự "
    "nhiên, 1-2 câu, dùng 'bạn' (KHÔNG 'thưa anh/chị', KHÔNG 'quý khách', "
    "KHÔNG 'dạ thưa'). KHÔNG ký tên, KHÔNG đính kèm chữ ký '— Demo Shop' "
    "hay tên shop ở cuối. Có thể chèn 1 emoji phù hợp (📱✨🔥) nếu hợp ngữ "
    "cảnh, không lạm dụng.\n\n"
    "Quy tắc về sản phẩm: chỉ nói có/giá những máy CÓ trong danh sách kho "
    "được cung cấp. Nếu khách hỏi máy không có trong kho thì trả lời "
    "'hiện chưa có hàng đó, có máy [gợi ý gần nhất] nếu bạn quan tâm' — "
    "KHÔNG bịa giá, BH, tình trạng. Khi muốn chốt đơn, mời inbox / để lại "
    "số điện thoại.\n\n"
    "TIN dữ liệu kho 100%. Tên model trong kho là chính xác — KHÔNG được "
    "nói 'tên này không tồn tại', 'Apple chưa ra mắt máy này', 'có nhầm "
    "không?'. Training data của bạn có thể lỗi thời."
)


# Allowed comment classification labels. Anything outside this set is
# coerced to "neutral" so callers can rely on a closed vocabulary.
COMMENT_LABELS = {
    "positive",      # cảm ơn, khen, ủng hộ
    "neutral",       # bình luận trung tính, chia sẻ
    "question",      # hỏi giá / hỏi sản phẩm / hỏi tư vấn
    "negative",      # phàn nàn dịch vụ / sản phẩm (feedback hợp lệ)
    "spam_toxic",    # spam, quảng cáo, chửi bới, nội dung cấm
}


CLASSIFY_PROMPT = (
    "Bạn là bộ phân loại bình luận Facebook của Demo Shop (cửa hàng điện thoại "
    "Apple cũ). Phân loại bình luận khách hàng vào ĐÚNG MỘT trong các nhãn:\n"
    "- positive: cảm ơn, khen ngợi, phản hồi tích cực\n"
    "- neutral: bình luận trung tính, chia sẻ kinh nghiệm, không đòi hỏi gì\n"
    "- question: hỏi giá / hỏi sản phẩm / hỏi tư vấn / cần thông tin\n"
    "- negative: phàn nàn, thất vọng, feedback tiêu cực CÓ LÝ DO (giao hàng "
    "chậm, máy lỗi, dịch vụ kém)\n"
    "- spam_toxic: spam, quảng cáo trang khác, link rác, chửi bới tục tĩu, "
    "nội dung cấm, công kích cá nhân, chửi shop vô cớ\n\n"
    "Trả về JSON đúng schema: "
    '{"label": "<một trong các nhãn>", "confidence": 0.0-1.0, "reason": '
    '"<giải thích ngắn 1 câu tiếng Việt>"}\n'
    "KHÔNG thêm văn bản nào ngoài JSON."
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
    return await _chat_completion(payload)


async def classify_comment(text: str) -> dict:
    """Classify a Page comment. Returns ``{label, confidence, reason}``.

    Falls back to ``{"label": "neutral", "confidence": 0.0, ...}`` on
    LLM/network failure so downstream code can stay simple.
    """
    fallback = {
        "label": "neutral",
        "confidence": 0.0,
        "reason": "classifier unavailable",
    }
    if not settings.openai_api_key:
        return fallback

    payload = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": CLASSIFY_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }
    raw = await _chat_completion(payload)
    if not raw:
        return fallback
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("classify_comment: non-JSON response %r", raw[:120])
        return fallback

    label = str(data.get("label", "")).strip().lower()
    if label not in COMMENT_LABELS:
        log.warning("classify_comment: unknown label %r — coerce to neutral", label)
        label = "neutral"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "label": label,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(data.get("reason", "")).strip(),
    }


async def _chat_completion(payload: dict) -> str:
    """POST to /chat/completions and return ``message.content`` (or "")."""
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    base = settings.openai_base_url.rstrip("/")
    url = f"{base}/chat/completions"

    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            log.error("LLM error %s: %s", r.status_code, r.text)
            return ""
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
