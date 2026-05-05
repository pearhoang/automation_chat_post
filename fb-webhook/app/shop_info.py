"""Static shop metadata (address, hours, shipping, deposit, bank).

Stored as ``/opt/fb-webhook/shop_info.json`` (mutable on the server) so
the owner can tweak values without redeploying. The defaults below are
placeholders — they're shown to the LLM as "chưa cấu hình" so it knows
to ask the customer to wait rather than fabricate an address.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SHOP_INFO_PATH = Path("/opt/fb-webhook/shop_info.json")

DEFAULTS: dict[str, str] = {
    "shop_name": "Apple Shop Siêu Lướt",
    "owner_name": "Hoàng",
    "address": "(chưa cấu hình)",
    "hours": "(chưa cấu hình)",
    "shipping": "(chưa cấu hình)",
    "deposit_policy": "(chưa cấu hình)",
    "bank_account": "(chưa cấu hình)",
    "warranty_default": "(chưa cấu hình)",
    "installment": "(chưa cấu hình)",
    "discount_policy": (
        "Giá đã tốt, không tự ý giảm; có thể tặng kèm cường lực + ốp "
        "khi khách kì kèo nhiều."
    ),
}


def load_shop_info() -> dict[str, str]:
    """Load shop_info.json + merge over the defaults."""
    out = dict(DEFAULTS)
    if SHOP_INFO_PATH.is_file():
        try:
            data = json.loads(SHOP_INFO_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, str) and v.strip():
                        out[k] = v.strip()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("shop_info: cannot read %s: %s", SHOP_INFO_PATH, exc)
    return out


def shop_info_block() -> str:
    """Render shop_info as a compact block for LLM context."""
    info = load_shop_info()
    lines = ["Thông tin shop (dùng khi khách hỏi):"]
    label = {
        "address": "địa chỉ",
        "hours": "giờ mở cửa",
        "shipping": "ship",
        "deposit_policy": "cọc giữ máy",
        "bank_account": "TK ngân hàng",
        "warranty_default": "BH mặc định",
        "installment": "trả góp",
        "discount_policy": "chính sách giảm giá",
    }
    for k, label_text in label.items():
        v = info.get(k, "")
        if not v:
            continue
        lines.append(f"  {label_text}: {v}")
    lines.append(
        "Nếu giá trị là '(chưa cấu hình)' thì tránh bịa — trả lời "
        "'shop nhắn lại bạn sau nhé' và cuộc trò chuyện sẽ được ping "
        "cho admin."
    )
    return "\n".join(lines)
