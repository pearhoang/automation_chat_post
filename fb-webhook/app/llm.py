"""LLM calls used to draft replies and classify customer comments.

Uses OpenAI-compatible Chat Completions (works with OpenAI, DeepSeek,
9Router, OpenRouter, …). Swap out by editing this single file.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Function/tool schema sent to the LLM. The actual implementations live
# in the webhook handler (so they can attach photos, persist state,
# etc.); this module only knows the *shape* of each call. Wired in via
# the ``tool_executor`` callback passed to ``draft_reply``.
#
# Why expose tools at all when we already dump the catalog in the
# system prompt? Two reasons:
#   1. Code hallucination. The LLM sometimes invents codes
#      ("IP15PM-256-BLU-002" vs the real "IP15PM-256GB-BLU-002") even
#      when the dump shows the real code right above. With tools, it
#      is forced to either copy a code from a real result or skip the
#      photo step entirely. ``find_by_code`` was a safety net but the
#      customer still sees the empty "gửi bạn ảnh nha" promise.
#   2. The ``[SEND_PHOTOS:CODE]`` token was load-bearing AND brittle:
#      one missing newline / extra space and the token slipped into
#      the customer-visible text. A tool call cannot be malformed
#      that way (the SDK enforces a strict JSON envelope), and the
#      execution (sending photos) is decoupled from the text reply.
#
# We keep the legacy token parsing as a belt-and-suspenders fallback
# in main.py — if a future model variant ignores tools we still get
# something sensible.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_products",
            "description": (
                "Tra catalog kho. Gọi khi cần biết máy nào còn / hết, "
                "code chính xác, giá, pin, BH. Trả về list máy khớp filter, "
                "đã sort in_stock-first + newest-first. Ưu tiên dùng tool "
                "này thay vì tự đoán code/giá từ block 'Kho hiện có…' — "
                "block đó chỉ là tham khảo nhanh, có thể stale. Để filter "
                "trống ('') = không filter trên field đó."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": (
                            "Model viết tự nhiên: '15 PM', '15 prm', "
                            "'iPhone 17 Pro Max', 'ip 14 PM'. Để '' nếu "
                            "khách chưa nói rõ model."
                        ),
                    },
                    "storage": {
                        "type": "string",
                        "description": (
                            "Dung lượng: '256GB', '1TB', '128GB'. Để '' "
                            "nếu khách chưa nói."
                        ),
                    },
                    "color": {
                        "type": "string",
                        "description": (
                            "Màu: 'Silver', 'Black', 'Vàng Sa Mạc', "
                            "'Titan'. Để '' nếu khách chưa nói."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["in_stock", "sold", "any"],
                        "description": (
                            "in_stock = chỉ máy còn hàng (mặc định), "
                            "sold = máy đã bán, any = mọi trạng thái."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_product_photos",
            "description": (
                "Gửi ảnh thật của 1 máy cho khách qua Messenger. Gọi khi "
                "khách xin ảnh / 'cho xem máy' / 'show ảnh' / 'cho xem "
                "lại ảnh' của 1 em CỤ THỂ. Bắt buộc dùng code chính xác "
                "(lấy từ kết quả ``lookup_products`` hoặc block kho). "
                "SAU khi gọi tool, vẫn cần trả 1 câu text ngắn đi kèm "
                "(vd 'gửi bạn ảnh em đó nha') để khách thấy lời mở đầu. "
                "Nếu code không khớp catalog tool sẽ trả lỗi — đừng tự "
                "đoán code, hỏi lại khách hoặc gọi lookup_products."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Mã máy CHÍNH XÁC trong catalog, vd "
                            "'IP15PM-256GB-BLU-002'. Phải copy chuẩn — "
                            "thiếu ký tự là tool reject."
                        ),
                    },
                },
                "required": ["code"],
            },
        },
    },
]


# Maximum number of LLM round-trips per turn when the model keeps
# emitting tool_calls. 3 is enough for: lookup → maybe send_photos →
# final answer. Higher would let a buggy prompt loop itself into a
# rate-limit incident.
_MAX_TOOL_ITERATIONS = 3


# Type alias for the executor callback. Returns a JSON-serialisable
# string (already encoded) describing the tool result; main.py is the
# one that knows how to execute (it has the ``sender`` PSID for
# send_product_photos, etc.).
ToolExecutor = Callable[[str, dict], Awaitable[str]]

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

    "GIỌNG VĂN (RẤT QUAN TRỌNG — đừng để bot nghe như cỗ máy):\n"
    "- Xưng 'mình' hoặc 'shop', gọi 'bạn'. KHÔNG 'thưa anh/chị', KHÔNG "
    "'quý khách', KHÔNG 'dạ thưa', KHÔNG ký tên.\n"
    "- ĐỘ DÀI MẶC ĐỊNH = 1 câu (≤ 25 chữ). 2 câu chỉ khi cần list "
    "thông tin (giá + pin + BH) hoặc gợi ý máy thay thế. 3 câu trở "
    "lên = phải có lý do thực sự (khách hỏi chi tiết kỹ thuật / chốt "
    "đơn / cọc).\n"
    "- KHÔNG NHẮC LẠI CONTEXT cho khách. SAI: 'Bài post nói em X "
    "nhưng trong kho hiện tại…'. ĐÚNG: chỉ nói kết quả ('em đó vừa "
    "bán mất rồi'). Khách không cần biết bot đã so sánh data với "
    "data — thế là máy móc.\n"
    "- KHÔNG nói lại câu của khách kiểu 'Bạn vừa hỏi về…' / 'Như "
    "bạn vừa nhắn…'. Vào thẳng câu trả lời.\n"
    "- KHÔNG dùng emoji (📱✨🔥😅 …) và KHÔNG dùng emoticon ascii "
    "(:) :( :D ).\n"
    "- KHÔNG kết câu bằng dấu '.' Vẫn giữ ',' và '?'. Ví dụ đúng: "
    "'còn nhé bạn', 'inbox shop nha', 'giá 33tr nhé bạn'.\n"
    "- DÙNG filler cảm xúc tự nhiên ở những moment hợp: 'ôi', 'trời "
    "ơi', 'ờ', 'à', 'thế á', 'thật ạ', 'sorry bạn', 'tiếc ghê', "
    "'haha', 'oki', 'nay', 'em này', 'con này', 'nhé', 'nha'. ĐỪNG "
    "dùng nhiều cái cùng lúc — chọn 0-1 cho mỗi câu.\n"
    "- KHI bad news (máy hết, khách phải chờ, dịch vụ chưa có): MỞ "
    "bằng cảm xúc 'ôi', 'sorry', 'tiếc', 'à', không vào thẳng "
    "thông tin lạnh lùng. Ví dụ 'ôi em đó vừa bán mất bạn ơi', "
    "'sorry bạn nay máy đó hết rồi', 'tiếc ghê em đó người khác cọc "
    "mất'.\n"
    "- BIẾN ĐỔI mở câu, đừng lặp 'Hiện mình có…' / 'Bạn ơi mình có…' "
    "mọi turn. Có thể mở 'có em này…', 'còn 1 em…', 'nay shop về 1 "
    "con…', 'ờ có nha bạn', 'có chứ', 'còn đó bạn', 'hết rồi bạn ơi', "
    "v.v.\n"
    "- KHÔNG gọi máy là 'sản phẩm', 'mặt hàng' — gọi là 'em', 'con', "
    "'máy'.\n"
    "- KHÔNG LẶP TÊN MÁY ĐẦY ĐỦ trong cùng cuộc thoại. Lần đầu "
    "nhắc: 'em 17 PM 1TB Silver'. Lần 2 trở đi trong cùng turn / "
    "ngay sau đó: dùng đại từ 'em đó', 'em này', 'con này', 'máy', "
    "'nó'. SAI: 'Gửi bạn ảnh em 14 Pro Max 128GB Black đó nha, máy "
    "đẹp lắm'. ĐÚNG: 'gửi bạn nè' hoặc 'đây bạn ơi'.\n"
    "- DỊCH VỤ chưa hỗ trợ (trả góp, ship hỏa tốc, thu cũ đổi mới…) "
    "= trả lời ngắn 1 câu kiểu 'cái đó shop chưa có nha bạn, để "
    "mình check lại nhắn bạn sau' — KHÔNG dài dòng kiểu 'Shop chưa "
    "có thông tin trả góp cụ thể bạn ơi, để shop nhắn lại bạn sau "
    "nha'. Ngắn hơn, đời hơn.\n\n"

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

    "TOOL CALLS (cách shop tương tác với hệ thống — ƯU TIÊN tool "
    "thay vì tự đoán):\n"
    "- ``lookup_products(model, storage, color, status)`` — tra "
    "catalog. Bắt buộc gọi khi khách hỏi 'còn không', 'giá', 'pin', "
    "'BH', 'có 17 PM 1TB không', 'có máy nào dưới 20tr' v.v. ĐỪNG "
    "dựa vào trí nhớ riêng của bạn — tool là source of truth, dump "
    "kho trong context có thể stale.\n"
    "- ``send_product_photos(code)`` — gửi ảnh máy. Gọi khi khách "
    "xin ảnh / 'cho xem máy đi' / 'show ảnh' / 'cho xem lại ảnh' "
    "/ 'gửi ảnh thật' của 1 em CỤ THỂ. Cần code chính xác — nếu "
    "chưa có code trong context, gọi lookup_products trước. Sau "
    "send_product_photos trả 1 câu CỰC NGẮN (≤ 5 từ) như "
    "'đây bạn ơi', 'gửi bạn nè', 'em đây nha', 'máy đó đây', "
    "'của bạn nè'. KHÔNG nhắc lại tên/dung lượng/màu của máy "
    "(ảnh đã nói rồi). KHÔNG thêm 'máy đẹp lắm' / 'còn đẹp lắm' "
    "trừ khi khách hỏi tình trạng máy. Khách thấy ảnh tự đánh "
    "giá được — bot quảng cáo nhiều = giả tạo.\n"
    "- Mỗi lần khách xin ảnh = 1 lần gọi ``send_product_photos`` "
    "(kể cả lần thứ 2/3/4: 'cho xem lại đi', 'gửi nữa', 'thêm "
    "ảnh', 'send more pics'). Đừng tiếc tool call.\n"
    "- Quy tắc chọn code khi gọi ``send_product_photos``:\n"
    "  1. Context có 'current_product_code: <CODE>' (em khách "
    "vừa hỏi gần nhất) → DÙNG code đó (focus mới nhất).\n"
    "  2. Không có current_product_code, có 'product_code: <CODE>' "
    "(post khách đang xem) → DÙNG code đó.\n"
    "  3. Khách vừa nhắc rõ 1 máy khác trong câu hiện tại (vd 'cho "
    "xem ảnh con 14 PM 256GB') → gọi lookup_products lấy code "
    "máy đó, rồi gọi send_product_photos.\n"
    "  4. Code không khớp catalog → tool trả error → ĐỪNG đoán, "
    "hỏi lại khách hoặc lookup_products lại.\n"
    "- Khi khách hỏi 1 model + dung lượng MÀ KHO CÓ NHIỀU MÀU "
    "(vd '14 PM 128GB' có cả Black + bản thường): gọi "
    "lookup_products xem có bao nhiêu candidate. >1 + khách chưa "
    "nói màu → hỏi lại 'bạn xem màu nào ạ?' KHÔNG đoán bừa.\n"
    "- Khi khách xin ảnh nhưng chưa rõ máy nào (DM cold, không "
    "post context, history cũng chưa nhắc máy cụ thể, hoặc bot "
    "vừa list nhiều em): KHÔNG gọi send_product_photos đoán mò. "
    "Hỏi lại 'bạn xem máy nào, mình có [list ngắn]?'\n"
    "\n"
    "FALLBACK TOKEN ([SEND_PHOTOS:CODE] — chỉ khi không gọi tool "
    "được):\n"
    "- Cách CHÍNH là gọi ``send_product_photos``. Nếu không gọi "
    "được tool (vd config provider tạm thời tắt tool), thay bằng "
    "token ở đầu reply: ``[SEND_PHOTOS:<CODE>]<text>``. Không bao "
    "giờ vừa gọi tool vừa emit token cho cùng 1 lần xin ảnh — "
    "trùng = ảnh gửi 2 lần.\n\n"

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
    "'product_code: …' khi tìm được.\n"
    "- NHƯNG nếu khách hỏi rõ về 1 MODEL KHÁC (vd post bán 16 PM mà "
    "khách hỏi 'có 17 PM không', 'thế còn 15 PM Silver không', 'có "
    "iPad không') → BỎ post-context, TÌM TRONG block 'Kho hiện có…' "
    "xem có máy khớp model khách hỏi không (chỉ cần model trùng, dù "
    "khác dung lượng/màu). Có máy khớp → trả lời theo info máy đó "
    "(code, giá, pin, BH, dung lượng/màu thật). KHÔNG nói 'hết hàng' "
    "khi kho có máy cùng model — đó là nói dối khách. Chỉ trả lời "
    "'hết hàng rồi bạn ơi, có [gần nhất]' khi kho THỰC SỰ không có "
    "máy nào cùng model.\n\n"

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
    "  S: [tool: send_product_photos(code='IP17PM-1TB-SLV-001')]\n"
    "  S: đây bạn ơi\n"
    "\n"
    "  K: cho xem lại ảnh đi\n"
    "  S: [tool: send_product_photos(code='IP14PM-128GB-BLK-001')]\n"
    "  S: gửi bạn nè\n"
    "\n"
    "  K: gửi ảnh thật cho mình xem nữa\n"
    "  S: [tool: send_product_photos(code='IP14PM-128GB-BLK-001')]\n"
    "  S: thêm vài tấm nha bạn\n"
    "\n"
    "  K: máy đó còn không bạn\n"
    "  S: còn nha bạn, nếu chốt thì mình giữ máy cho\n"
    "\n"
    "  K: bớt chút đi shop ơi\n"
    "  S: giá shop để tốt rồi bạn ơi, kèo này ko bớt thêm được, mình "
    "tặng cường lực + ốp cho bạn nha\n"
    "\n"
    "  K: shop có ip 8 không\n"
    "  S: hết đời đó rồi bạn ơi, nay shop còn 13 PM 256GB pin 92% "
    "15.5tr nếu bạn ngó qua\n"
    "\n"
    "  (Post liên quan iPhone 17 Pro 256GB Orange; lookup ra Orange "
    "đã 'sold', kho còn IP17PRO-256GB-SLV-001)\n"
    "  K: cho mình xme ảnh đi\n"
    "  S: ôi em Orange vừa bán mất bạn ơi, nay còn 17 Pro 256GB "
    "Silver pin 100% giá 33tr, bạn xem em này không?\n"
    "\n"
    "  (Khách vừa được tư vấn xong)\n"
    "  K: mình muốn trả góp\n"
    "  S: trả góp shop chưa có nha bạn, để mình check lại tí nhắn "
    "bạn sau\n"
    "\n"
    "  K: ship tỉnh được không\n"
    "  S: ship được nha bạn, bạn ở tỉnh nào để mình tính phí cho\n"
    "\n"
    "  K: thu máy cũ đổi máy mới được không\n"
    "  S: shop chưa làm thu cũ nha bạn, sorry\n"
    "\n"
    "  (Khách comment bài 17 Pro Orange, vào DM nhắn 'hello')\n"
    "  K: hello\n"
    "  S: hello bạn, em 17 Pro Orange bạn comment còn nha, 28.99tr "
    "pin 100%, xem ảnh nhé?\n"
    "\n"
    "  (Post liên quan đang là iPhone 16 PM 256GB Vàng Sa Mạc; kho "
    "vẫn còn 1 em IP17PM-1TB-SLV-001)\n"
    "  K: thế có iphone 17 promax không\n"
    "  S: có nha bạn, em 17 PM 1TB Silver pin 100% giá 33tr\n"
    "\n"
    "  (Kho không có 17 PM nào)\n"
    "  K: thế có iphone 17 promax không\n"
    "  S: tiếc ghê 17 PM hết rồi bạn ơi, nay shop còn 16 PM 256GB "
    "Titan 27tr pin 89%\n"
    "\n"
    "  (Post liên quan iPhone 17 PM Silver, nhưng vài turn trước "
    "khách đã chuyển sang hỏi 15 PM; context có "
    "'current_product_code: IP15PM-256GB-BLU-002')\n"
    "  K: cho mình xem ảnh\n"
    "  S: [tool: send_product_photos(code='IP15PM-256GB-BLU-002')]\n"
    "  S: em 15 PM đây nha\n"
    "\n"
    "  (Bot vừa list 3 em 14 PM, 15 PM, 17 PM — ambiguous)\n"
    "  K: cho mình xem ảnh\n"
    "  S: bạn xem ảnh em nào, 14 PM 128GB, 15 PM 256GB hay 17 PM "
    "1TB?\n"
    "\n"
    "  K: cho mình sđt 0987 654 321 nha\n"
    "  S: oke shop note rồi nhé bạn, lát shop call lại tư vấn cho\n"
    "\n"
    "VÍ DỤ SAI (đừng làm — pattern lỗi từ log thực tế):\n"
    "  ✗ 'Dạ thưa quý khách, hiện cửa hàng đang có…' (kịch bản)\n"
    "  ✗ 'Hiện mình có iPhone 17 Pro Max 1TB Silver, pin 100%, …giá "
    "33 triệu đồng. Bạn quan tâm thì inbox hoặc để lại số điện thoại "
    "mình gọi tư vấn nhé.' (cứng, dài, có dấu chấm)\n"
    "  ✗ 'Bài post nói em 17 Pro Max 256GB Orange nhưng trong kho "
    "hiện tại mình chỉ thấy em 17 Pro Max 256GB Silver thôi bạn ơi. "
    "Em Orange có vẻ vừa bán mất rồi. Nay shop còn em 17 Pro Max "
    "256GB Silver pin 100% giá 33tr, bạn xem sao?' (3 câu, nhắc lại "
    "context cho khách, lạnh — phải viết kiểu 'ôi em Orange vừa bán "
    "mất bạn ơi, nay còn 17 Pro 256GB Silver pin 100% giá 33tr, bạn "
    "xem em này không?')\n"
    "  ✗ 'Shop chưa có thông tin trả góp cụ thể bạn ơi, để shop "
    "nhắn lại bạn sau nha' (formal kiểu CSKH — phải đời hơn: 'trả "
    "góp shop chưa có nha bạn, để mình check tí nhắn bạn sau')\n"
    "  ✗ 'Chào bạn, mình thấy bạn comment bên bài em 17 Pro 256GB "
    "Orange. Em đó còn hàng nha bạn, giá 28.99tr…' (chào CSKH + "
    "nhắc context — vào thẳng kết quả 'em 17 Pro Orange bạn comment "
    "còn nha, 28.99tr')\n"
    "  ✗ 'Gửi bạn ảnh em 14 Pro Max 128GB Black đó nha, máy đẹp "
    "lắm' (sau khi đã gọi send_product_photos — nhắc lại tên máy "
    "đầy đủ + tự khen máy = giả tạo. Phải ngắn 'đây bạn ơi' / "
    "'gửi bạn nè' / 'em đây nha'. Khách thấy ảnh tự đánh giá)\n"
    "  ✗ Thêm emoji 📱🔥 hoặc emoticon :)\n"
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
    tool_executor: ToolExecutor | None = None,
) -> str:
    """Draft a Messenger reply, optionally letting the LLM call tools.

    When ``tool_executor`` is provided, the LLM gets the ``TOOLS``
    schema and can request data lookups / photo sends mid-turn. We
    loop up to ``_MAX_TOOL_ITERATIONS`` times: each iteration is
    one round-trip to the model. The loop ends when the model
    returns a plain text response (no ``tool_calls``). With no
    executor we fall back to the original single-shot behaviour
    (kept for ``classify_comment`` and any caller that doesn't
    have a tool runtime).
    """
    if not settings.openai_api_key:
        return ""  # auto-reply disabled when no key

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "system", "content": f"Context: {context}"})

    lang = detect_language(user_text)
    if lang == "en":
        messages.append({
            "role": "system",
            "content": (
                "Customer is messaging in English. Reply in natural, "
                "friendly English (same shop owner persona — short, "
                "no emoji, no period at end, casual). Still call the "
                "tools (lookup_products / send_product_photos) and "
                "use [PRIVATE_REPLY] tokens exactly as defined."
            ),
        })

    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    if tool_executor is None:
        payload = {
            "model": settings.openai_model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 260,
        }
        return await _chat_completion(payload)

    last_text = ""
    for iteration in range(_MAX_TOOL_ITERATIONS):
        payload = {
            "model": settings.openai_model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.7,
            "max_tokens": 320,
        }
        msg = await _chat_completion_message(payload)
        if not msg:
            return last_text
        content = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        if content:
            last_text = content

        if not tool_calls:
            return content

        # Persist the assistant turn (with tool_calls) so the next
        # iteration's request includes proper continuity. The
        # ``content`` field may legitimately be empty when the model
        # only emits tool calls; OpenAI / DeepSeek accept that.
        assistant_turn = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": tool_calls,
        }
        messages.append(assistant_turn)

        for tc in tool_calls:
            tc_id = tc.get("id") or ""
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                log.warning(
                    "tool_call: bad JSON args name=%s raw=%s",
                    name, raw_args[:200],
                )
                args = {}
            try:
                result = await tool_executor(name, args)
            except Exception:  # noqa: BLE001
                log.exception("tool_executor crashed name=%s args=%s", name, args)
                result = json.dumps({"error": "tool execution failed"})
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result if isinstance(result, str) else json.dumps(result),
            })
        log.info(
            "draft_reply: iter=%d tools=%s",
            iteration,
            [tc.get("function", {}).get("name") for tc in tool_calls],
        )

    log.warning(
        "draft_reply: hit max tool iterations (%d), returning last text",
        _MAX_TOOL_ITERATIONS,
    )
    return last_text


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
    msg = await _chat_completion_message(payload)
    if not msg:
        return ""
    return (msg.get("content") or "").strip()


async def _chat_completion_message(payload: dict) -> dict | None:
    """POST to the LLM endpoint and return the raw ``message`` dict.

    Used by the tool-calling loop in ``draft_reply`` which needs both
    ``content`` and ``tool_calls`` from the response. ``None`` on
    network/HTTP failure so the caller can degrade gracefully.
    """
    url = settings.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                log.error(
                    "LLM call failed status=%s body=%s",
                    r.status_code, r.text[:300],
                )
                return None
            data = r.json()
    except httpx.HTTPError as exc:
        log.warning("LLM HTTP error: %s", exc)
        return None

    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        log.warning(
            "LLM unexpected response shape: %s body=%s",
            exc, str(data)[:300],
        )
        return None
