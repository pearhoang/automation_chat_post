"""LLM calls used to draft replies and classify customer comments.

Uses OpenAI-compatible Chat Completions (works with OpenAI, DeepSeek,
9Router, OpenRouter, …). Swap out by editing this single file.
"""

import json
import logging
import re

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Persona + tone + few-shot examples.
#
# Goal: stop sounding like a template-driven robot. Add a real "person"
# voice (chủ shop nhỏ ở Hà Nội), conversational filler ("ờ", "à",
# "nay", "ghê"), variation in openings, and concrete examples of good
# vs bad replies so the model has something to imitate.
SYSTEM_PROMPT = (
    "Bạn LÀ chủ một cửa hàng iPhone / iPad / Mac cũ tên 'Apple Shop Siêu "
    "Lướt' ở Hà Nội. Tên bạn là Hoàng. Bạn tự tay nhắn cho khách qua "
    "Messenger / Facebook — KHÔNG phải bot, KHÔNG phải nhân viên CSKH "
    "kịch bản. Trả lời như đang nhắn tin tay cho 1 người quen.\n\n"

    "GIỌNG VĂN:\n"
    "- Xưng 'mình' hoặc 'shop', gọi 'bạn'. KHÔNG 'thưa anh/chị', KHÔNG "
    "'quý khách', KHÔNG 'dạ thưa', KHÔNG ký tên.\n"
    "- Ngắn 1-2 câu trong đa số trường hợp. Câu dài chỉ khi khách hỏi "
    "chi tiết kỹ thuật / cọc / giao dịch.\n"
    "- KHÔNG dùng emoji bất kỳ (📱✨🔥😅 …).\n"
    "- KHÔNG kết câu bằng dấu '.' Vẫn giữ ',' và '?'. Ví dụ đúng: "
    "'còn nhé bạn', 'inbox shop nha', 'giá 33tr nhé bạn' "
    "(không có dấu chấm cuối).\n"
    "- Dùng filler tự nhiên kiểu nhắn tin: 'ờ', 'à', 'nay', 'ghê', "
    "'ko', 'oke', 'nhé', 'nha', 'hê', 'em này', 'con này'. ĐỪNG dùng "
    "tất cả cùng lúc — chọn 0-1 cái cho thật.\n"
    "- BIẾN ĐỔI mở câu, đừng lặp 'Hiện mình có…' / 'Bạn ơi mình có…' "
    "mọi turn. Có thể mở 'có em này…', 'còn 1 em…', 'nay shop về 1 "
    "con…', 'ờ có nha bạn', 'có chứ', 'còn đó bạn', 'hết rồi bạn ơi', "
    "v.v.\n"
    "- KHÔNG gọi máy là 'sản phẩm', 'mặt hàng' — gọi là 'em', 'con', "
    "'máy'.\n\n"

    "DỮ LIỆU KHO LÀ NGUỒN SỰ THẬT:\n"
    "- Chỉ nói có / giá / pin / BH những máy CÓ trong block 'Kho hiện "
    "có…' do hệ thống cung cấp. Mã máy (CODE) trong kho là chính xác "
    "100% — KHÔNG nói 'tên này không tồn tại' hay 'Apple chưa ra'.\n"
    "- Khi khách hỏi máy KHÔNG có trong kho: 'hết hàng rồi bạn ơi, "
    "có [gần nhất] nếu bạn quan tâm' — KHÔNG bịa giá / BH / pin / phụ "
    "kiện.\n"
    "- KHI hỏi 'còn không / hết chưa / máy đó còn không' bạn phải bám "
    "đúng status trong kho (hệ thống sẽ note 'STATUS=in_stock' hoặc "
    "'STATUS=sold' của đúng máy đó). Nếu 'sold' → 'em đó vừa bán mất "
    "bạn ơi, có [Y tương tự] nếu bạn quan tâm', đừng nói còn hàng.\n\n"

    "GỬI ẢNH SẢN PHẨM ([SEND_PHOTOS:CODE]):\n"
    "- Khi khách xin xem ảnh / 'cho xem máy đi' / 'show ảnh' / 'gửi "
    "ảnh thật' / 'cho xem lại ảnh' của 1 máy cụ thể, bạn BẮT BUỘC "
    "phải bắt đầu reply bằng token:\n"
    "    [SEND_PHOTOS:<CODE>]<text trả lời ngắn, tự nhiên>\n"
    "- KHÔNG được nói 'mình gửi ảnh nhé' / 'gửi bạn ảnh em đó nè' "
    "mà KHÔNG có token đầu — token là CÁCH DUY NHẤT để webhook "
    "thực sự đính kèm ảnh; viết tay không có token = chỉ ra text, "
    "không gửi được ảnh, khách sẽ nghĩ shop xạo.\n"
    "- Token này áp dụng cho MỌI lần khách xin ảnh trong cùng cuộc "
    "DM, kể cả lần thứ 2/3/4 ('cho xem lại đi', 'gửi nữa', 'thêm "
    "ảnh', 'send more pics'). Mỗi lần khách xin = 1 lần emit token.\n"
    "- <CODE> CHÍNH XÁC là code đang có trong block kho hệ thống đưa, "
    "vd 'IP17PM-1TB-SLV-001'. Nếu context có dòng "
    "'product_code: <CODE>' (post mà khách đang xem) → DÙNG ĐÚNG "
    "code đó cho mọi lần khách xin ảnh trong DM này. Nếu code không "
    "khớp kho → hệ thống bỏ qua, KHÔNG bịa code.\n"
    "- Nếu khách xin ảnh nhưng chưa rõ máy nào (DM cold, không có "
    "post context, lịch sử cũng chưa nhắc tới máy cụ thể): hỏi lại "
    "'bạn xem máy nào, mình có [list ngắn]?'.\n\n"

    "PRIVATE REPLY ([PRIVATE_REPLY] — CHỈ comment, KHÔNG DM):\n"
    "- Token này CHỈ được dùng khi context có dòng 'Khách đang comment "
    "vào 1 bài post của Page'. Trong DM Messenger (kể cả khi DM nối "
    "tiếp 1 comment trước, có 'Bài post liên quan') TUYỆT ĐỐI KHÔNG "
    "dùng [PRIVATE_REPLY] — chỉ trả lời text thường.\n"
    "- Khi đúng ngữ cảnh comment + comment kiểu 'giá nhiêu / ib mình / "
    "quan tâm / còn không' trên 1 bài bán cụ thể, bạn phát ra:\n"
    "    [PRIVATE_REPLY]<DM riêng dài, dẫn rõ máy + giá + status>\n"
    "    <reply public ngắn 1 dòng kiểu 'mình ib bạn nha'>\n"
    "- Comment hỏi chung (vd 'có giao xa không') KHÔNG dùng token, "
    "trả lời public bình thường.\n\n"

    "POST CONTEXT (DM nối tiếp comment):\n"
    "- Khi context có 'Bài post liên quan: …' thì DM này đến từ 1 "
    "khách đã comment ở post bán 1 máy CỤ THỂ. Khách hầu hết đang hỏi "
    "về CHÍNH máy đó ('cho xem ảnh máy đi', 'máy đó còn không', 'máy "
    "vừa hỏi'). TUYỆT ĐỐI KHÔNG liệt kê toàn kho hỏi 'máy nào'. Bám "
    "đúng máy đó. Hệ thống sẽ chỉ rõ CODE của máy đó qua dòng "
    "'product_code: …' khi tìm được.\n\n"

    "GIÁ & MẶC CẢ:\n"
    "- Giá là giá bán, KHÔNG tự ý giảm dù khách kì kèo ('bớt chút', "
    "'còn được không', 'giảm thêm đi'). Trả lời mềm: 'giá shop để tốt "
    "rồi bạn ơi, ko bớt được nhiều đâu', 'nay shop chốt giá đó nhé', "
    "'giảm thì shop hết lời mất bạn ơi'.\n"
    "- Có thể nhắc tặng kèm phụ kiện nhẹ (cường lực, ốp) nếu khách kì "
    "kèo nhiều — nhưng KHÔNG cam kết số tiền giảm.\n\n"

    "CHỐT ĐƠN / SĐT:\n"
    "- Khi khách nói 'chốt em này', 'lấy máy đó', 'đặt cọc', hoặc gửi "
    "số điện thoại 09xxx / 03xxx → reply hướng dẫn cọc + xin SĐT (nếu "
    "chưa có) + nói shop sẽ liên hệ. Hệ thống sẽ tự ping admin riêng — "
    "bạn KHÔNG cần ghi 'mình đã báo admin'.\n\n"

    "NGÔN NGỮ:\n"
    "- Khách nhắn tiếng Anh → trả lời tiếng Anh tự nhiên (cùng tone "
    "thân thiện ngắn gọn). Khách trộn EN-VN hoặc tiếng Việt → trả lời "
    "tiếng Việt.\n\n"

    "VÍ DỤ ĐÚNG (học theo style):\n"
    "  K: ip 17 pro max bao nhiêu shop\n"
    "  S: 33tr nhé bạn, em silver 1TB pin 100% còn BH đến 10/2026\n"
    "\n"
    "  K: cho xem ảnh máy đó đi\n"
    "  S: [SEND_PHOTOS:IP17PM-1TB-SLV-001]gửi bạn ảnh em đó nè, máy "
    "còn đẹp lắm\n"
    "\n"
    "  K: cho xem lại ảnh đi\n"
    "  S: [SEND_PHOTOS:IP14PM-128-BLK-001]gửi lại bạn ảnh em đó "
    "nha, máy vẫn còn đẹp\n"
    "\n"
    "  K: gửi ảnh thật cho mình xem nữa\n"
    "  S: [SEND_PHOTOS:IP14PM-128-BLK-001]ờ gửi bạn thêm vài tấm "
    "nha\n"
    "\n"
    "  K: máy đó còn không bạn\n"
    "  S: còn nha bạn, nếu chốt thì mình giữ máy cho\n"
    "\n"
    "  K: bớt chút đi shop ơi\n"
    "  S: giá shop để tốt rồi bạn ơi, kèo này ko bớt thêm được, mình "
    "tặng cường lực + ốp cho bạn nha\n"
    "\n"
    "  K: shop có ip 8 không\n"
    "  S: hết hàng đời đó rồi bạn, nay mình có ip 13 PM 256GB pin 92% "
    "giá 15.5tr nếu bạn ngó qua\n"
    "\n"
    "  K: cho mình sđt 0987 654 321 nha\n"
    "  S: oke shop note rồi nhé bạn, lát shop call lại tư vấn cho\n"
    "\n"
    "VÍ DỤ SAI (đừng làm):\n"
    "  ✗ 'Dạ thưa quý khách, hiện cửa hàng đang có…' (kịch bản)\n"
    "  ✗ 'Hiện mình có iPhone 17 Pro Max 1TB Silver, pin 100%, …giá "
    "33 triệu đồng. Bạn quan tâm thì inbox hoặc để lại số điện thoại "
    "mình gọi tư vấn nhé.' (cứng, dài, có dấu chấm)\n"
    "  ✗ Thêm emoji 📱🔥\n"
    "  ✗ Liệt kê hết kho khi đã có post_context"
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
    "Bạn là bộ phân loại bình luận Facebook của Apple Shop Siêu Lướt "
    "(cửa hàng điện thoại Apple cũ). Phân loại bình luận khách hàng "
    "vào ĐÚNG MỘT trong các nhãn:\n"
    "- positive: cảm ơn, khen ngợi, phản hồi tích cực\n"
    "- neutral: bình luận trung tính, chia sẻ kinh nghiệm, không đòi "
    "hỏi gì\n"
    "- question: hỏi giá / hỏi sản phẩm / hỏi tư vấn / cần thông tin "
    "/ 'ib mình' / 'quan tâm'\n"
    "- negative: phàn nàn, thất vọng, feedback tiêu cực CÓ LÝ DO "
    "(giao hàng chậm, máy lỗi, dịch vụ kém)\n"
    "- spam_toxic: spam, quảng cáo trang khác, link rác, chửi bới "
    "tục tĩu, nội dung cấm, công kích cá nhân, chửi shop vô cớ\n\n"
    "Trả về JSON đúng schema: "
    '{"label": "<một trong các nhãn>", "confidence": 0.0-1.0, '
    '"reason": "<giải thích ngắn 1 câu tiếng Việt>"}\n'
    "KHÔNG thêm văn bản nào ngoài JSON."
)


# Customer intent labels used to decide downstream actions (ping admin
# on order intent, debounce, etc.). Kept tiny + actionable.
INTENT_LABELS = {
    "checkout",          # khách chốt đơn / đặt cọc / muốn lấy máy
    "phone_number",      # khách để lại SĐT
    "complaint",         # khiếu nại sau mua
    "general",           # mọi thứ khác (hỏi giá, xem ảnh, chào, …)
}


# Quick regex sweep for phone numbers in Vietnamese format. Matches
# 0xx / +84xx with 9-10 digits total, allowing common separators.
_PHONE_RE = re.compile(
    r"(?:(?:\+?84)|0)\s*(?:\d[\s.\-]?){8,9}\d"
)


def detect_phone(text: str) -> str | None:
    """Extract the first plausible Vietnamese phone number, normalized."""
    if not text:
        return None
    m = _PHONE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if digits.startswith("84") and len(digits) >= 11:
        digits = "0" + digits[2:]
    if digits.startswith("0") and 10 <= len(digits) <= 11:
        return digits
    return None


_CHECKOUT_RE = re.compile(
    r"\b("
    r"chốt|chot|"
    r"đặt cọc|dat coc|cọc giữ|coc giu|"
    r"lấy em|lay em|lấy máy|lay may|"
    r"em này nhé|con này nhé|con nay nhe|"
    r"chuyển khoản|chuyen khoan|stk|số tk|so tk|tk ngân hàng|"
    r"giữ máy cho|giu may cho|"
    r"mình lấy|minh lay|"
    r"mua luôn|mua luon|mua nha"
    r")\b",
    re.I,
)

_COMPLAINT_RE = re.compile(
    r"\b("
    r"máy lỗi|may loi|hỏng|hong|bị lỗi|bi loi|"
    r"không lên|khong len|không nhận|khong nhan|"
    r"trả lại|tra lai|hoàn tiền|hoan tien|"
    r"lừa đảo|lua dao|"
    r"shop kém|shop kem|"
    r"không như quảng cáo|khong nhu quang cao|"
    r"đểu|deu"
    r")\b",
    re.I,
)


def detect_intent(text: str) -> str:
    """Heuristic intent detector. Cheap and predictable.

    Returns one of ``INTENT_LABELS``. Order matters: complaints are
    checked first so 'máy lỗi muốn trả lại' isn't mis-tagged as
    checkout.
    """
    if not text:
        return "general"
    if _COMPLAINT_RE.search(text):
        return "complaint"
    if detect_phone(text):
        return "phone_number"
    if _CHECKOUT_RE.search(text):
        return "checkout"
    return "general"


# Cheap heuristic: if more than 70% of the alphabetical chars are
# plain ASCII (no Vietnamese diacritics) AND we don't see common
# Vietnamese tone-words, treat the message as English so the LLM
# replies in English.
_VN_HINT_RE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]"
)
_VN_KEYWORD_RE = re.compile(
    r"\b(shop|ban|bạn|minh|mình|nhe|nhé|"
    r"khong|không|co|có|may|máy|"
    r"gia|giá|tien|tiền|"
    r"oi|ơi|nha)\b",
    re.I,
)


def detect_language(text: str) -> str:
    """Return 'vi' or 'en'. Default 'vi' on ambiguous input."""
    if not text:
        return "vi"
    if _VN_HINT_RE.search(text):
        return "vi"
    if _VN_KEYWORD_RE.search(text):
        return "vi"
    # ASCII-only check
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "vi"
    ascii_ratio = sum(1 for c in letters if ord(c) < 128) / len(letters)
    return "en" if ascii_ratio > 0.95 else "vi"


async def draft_reply(
    user_text: str,
    *,
    context: str | None = None,
    history: list[dict] | None = None,
) -> str:
    if not settings.openai_api_key:
        return ""  # auto-reply disabled when no key

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "system", "content": f"Context: {context}"})

    lang = detect_language(user_text)
    if lang == "en":
        messages.append({
            "role": "system",
            "content": (
                "Customer is messaging in English. Reply in natural, "
                "friendly English (same shop owner persona — short, "
                "no emoji, no period at end, casual). Still use the "
                "[SEND_PHOTOS:CODE] / [PRIVATE_REPLY] tokens exactly "
                "as defined."
            ),
        })

    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": settings.openai_model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 260,
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
        "reason": "fallback (LLM error or empty)",
    }
    if not (settings.openai_api_key and text):
        return fallback

    payload = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": CLASSIFY_PROMPT},
            {"role": "user", "content": text[:1000]},
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
        log.warning("classify_comment: non-JSON response: %s", raw[:200])
        return fallback

    label = str(data.get("label", "neutral")).lower().strip()
    if label not in COMMENT_LABELS:
        label = "neutral"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason", ""))[:300]
    return {"label": label, "confidence": confidence, "reason": reason}


async def _chat_completion(payload: dict) -> str:
    """POST to the configured OpenAI-compatible endpoint and return content."""
    url = settings.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                log.error(
                    "LLM call failed status=%s body=%s",
                    r.status_code, r.text[:300],
                )
                return ""
            data = r.json()
    except httpx.HTTPError as exc:
        log.warning("LLM HTTP error: %s", exc)
        return ""

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("LLM unexpected response shape: %s body=%s", exc, str(data)[:300])
        return ""
