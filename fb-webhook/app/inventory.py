"""Read-only access to the shop inventory catalog written by the
``inventory`` OpenClaw skill.

The skill keeps everything in
``$OPENCLAW_WORKSPACE/inventory/catalog.json`` (default
``~/.openclaw/workspace/inventory/catalog.json``). This module does NOT
write to the catalog — only the skill scripts do — so we never have to
worry about racing with the agent.

We re-read the file from disk every call. It is small (a few KB even
with a hundred phones) and the disk cache makes this effectively free,
but we always pick up fresh data without needing a restart after the
agent adds/removes a product.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)


def catalog_path() -> Path:
    """Resolve the catalog.json path from env, with a sane default."""
    workspace = os.environ.get("OPENCLAW_WORKSPACE") or str(
        Path.home() / ".openclaw" / "workspace"
    )
    return Path(workspace) / "inventory" / "catalog.json"


def load_catalog() -> dict:
    """Return the full catalog dict, or an empty stub on any error."""
    path = catalog_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "products": []}
    except json.JSONDecodeError:
        log.warning("catalog.json is not valid JSON at %s", path)
        return {"version": 1, "products": []}
    except OSError as exc:
        log.warning("cannot read catalog.json: %s", exc)
        return {"version": 1, "products": []}


def in_stock_products() -> list[dict]:
    return [
        p for p in load_catalog().get("products", [])
        if p.get("status") == "in_stock"
    ]


def find_by_code(code: str) -> dict | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    for p in load_catalog().get("products", []):
        if p.get("code", "").upper() == code:
            return p
    return None


def product_photos(code: str, *, max_photos: int = 5) -> list[Path]:
    """Return ordered list of photo paths for a product code, in_stock or sold."""
    code = (code or "").strip().upper()
    if not code:
        return []
    workspace = os.environ.get("OPENCLAW_WORKSPACE") or str(
        Path.home() / ".openclaw" / "workspace"
    )
    inv = Path(workspace) / "inventory"
    for sub in ("products", "sold"):
        folder = inv / sub / code
        if folder.is_dir():
            photos = sorted(
                p for p in folder.iterdir()
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            )
            return photos[:max_photos]
    return []


def all_products() -> list[dict]:
    """Return every product in the catalog (any status)."""
    return list(load_catalog().get("products", []))


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics + collapse whitespace for fuzzy match."""
    import unicodedata

    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(plain.lower().split())


def match_post_to_product(post_text: str) -> dict | None:
    """Find the catalog product most likely referred to by a post body.

    The post_text is the caption of a Page post (e.g. "iPhone 17 Pro
    256GB Orange bản Mỹ, sạc 47 lần, pin 100%, giá 28.99tr"). We pick
    the product whose model + storage + color all appear (substring,
    diacritic-insensitive) inside the normalized post text.

    Matching is intentionally lenient: we *require* model + storage to
    appear in the post body; ``color`` is a tie-breaker only. This
    avoids spurious misses when the catalog uses Vietnamese color
    names ("Đen", "Trắng") and the post caption uses English ones
    ("Black", "White") or vice-versa, OR the post simply omits the
    color (Apple Pages often write "iPhone 14 Pro Max 128GB chính
    hãng VN/A" without spelling out the color in the caption).

    Tie-breakers (lower is better):
      1. Color match (color present in haystack) wins over no-color
         match — so when two units differ only in color we still
         prefer the right one.
      2. ``in_stock`` beats ``sold``.
      3. Newer ``added_at`` first (recent listings beat stale ones).
    """
    if not post_text:
        return None
    haystack = _normalize(post_text)
    candidates: list[tuple[tuple, dict]] = []
    for p in all_products():
        model = _normalize(p.get("model", ""))
        storage = _normalize(p.get("storage", ""))
        color = _normalize(p.get("color", ""))
        if not (model and storage):
            continue
        if model not in haystack or storage not in haystack:
            continue
        color_match = bool(color and color in haystack)
        rank = (
            0 if color_match else 1,
            0 if p.get("status") == "in_stock" else 1,
            # Negative ASCII tuple of added_at sorts newer first.
            tuple(-ord(c) for c in (p.get("added_at") or "")),
        )
        candidates.append((rank, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _model_aliases(model: str) -> list[str]:
    """Common Vietnamese-shorthand names for an iPhone/iPad/Mac model.

    The catalog stores the long form ("iPhone 15 Pro Max"); customers
    chat in shorthand ("15 PM", "15 prm", "15pm", "ip 15 pm"). We try
    each alias when matching DM text so a switch like "có 15 PM không?"
    can still resolve to ``IP15PM-…`` even though the customer never
    typed the full name. Returned strings are already lowercased and
    diacritic-stripped (``_normalize`` form).
    """
    base = _normalize(model)
    if not base:
        return []
    aliases: set[str] = {base}
    no_brand = re.sub(
        r"^(iphone|ipad|macbook|mac|imac|airpods?|watch)\s+",
        "",
        base,
    )
    if no_brand:
        aliases.add(no_brand)
    # Pro Max ↔ PM ↔ PRM (Vietnamese shorthand). Customers also smush
    # the words ("15pm", "15PRM") so we register no-space variants too.
    if "pro max" in no_brand:
        for alt in ("pm", "prm", "promax"):
            spaced = no_brand.replace("pro max", alt)
            squished = spaced.replace(" ", "")
            aliases.add(spaced)
            aliases.add(squished)
            # also support "15 pm" with a digit prefix kept as-is
            aliases.add(no_brand.replace(" pro max", " " + alt))
            aliases.add(no_brand.replace(" pro max", alt))
    return sorted({a.strip() for a in aliases if a.strip()})


def find_products_in_text(text: str) -> list[dict]:
    """Return every catalog product the text uniquely resolves to.

    Used by the Messenger handler to figure out which product the
    *current* DM turn is referring to (vs the post product the DM
    thread originated from). Matching:

    1. Filter to products whose model alias + storage both appear
       in ``text`` (model fuzzy via ``_model_aliases``, storage
       strict via ``_normalize`` substring).
    2. If multiple candidates remain, narrow by color: keep only
       the ones whose ``color`` field appears in the text. So a bot
       reply like "16 Pro Max Vàng Sa Mạc 256GB" picks just the
       Vàng-Sa-Mạc SKU even though Titan also matches model+storage.
    3. If still multiple, return them all — the caller treats that
       as ambiguous and refuses to guess.

    Returns deduplicated by ``code``; in_stock entries first, then
    sold ones, newest first inside each group. Empty list when
    nothing matches model+storage.
    """
    if not text:
        return []
    haystack = _normalize(text)
    if not haystack:
        return []
    candidates: list[tuple[tuple, dict]] = []
    seen: set[str] = set()
    for p in all_products():
        code = (p.get("code") or "").upper()
        if not code or code in seen:
            continue
        storage = _normalize(p.get("storage", ""))
        if not storage or storage not in haystack:
            continue
        if not any(
            alias in haystack
            for alias in _model_aliases(p.get("model", ""))
        ):
            continue
        seen.add(code)
        rank = (
            0 if p.get("status") == "in_stock" else 1,
            tuple(-ord(c) for c in (p.get("added_at") or "")),
        )
        candidates.append((rank, p))
    if len(candidates) > 1:
        # Disambiguate by color when the text actually mentions it.
        color_hits = [
            (rank, p) for rank, p in candidates
            if (
                _normalize(p.get("color", ""))
                and _normalize(p.get("color", "")) in haystack
            )
        ]
        if color_hits:
            candidates = color_hits
    candidates.sort(key=lambda x: x[0])
    return [p for _, p in candidates]


def lookup_products(
    *,
    model: str | None = None,
    storage: str | None = None,
    color: str | None = None,
    status: str | None = "in_stock",
    max_results: int = 12,
) -> list[dict]:
    """Filter the catalog by free-text fields. Used by the LLM tool layer.

    The LLM passes Vietnamese-shorthand strings ("15 PM", "1TB", "Vàng
    Sa Mạc") and we match leniently using ``_normalize`` + alias
    expansion. ``status`` can be ``"in_stock"`` (default), ``"sold"``,
    or ``"any"``. Other filters are AND-ed together; an empty/None
    filter matches everything for that field.

    Returns a list of compact dicts (only the fields the LLM needs to
    answer the customer): ``code``, ``model``, ``storage``, ``color``,
    ``status``, ``price_vnd``, ``battery``, ``warranty_until``,
    ``added_at``. Sorted in_stock-first then newest-first.
    """
    model_q = _normalize(model or "")
    storage_q = _normalize(storage or "")
    color_q = _normalize(color or "")
    status_q = (status or "in_stock").lower().strip()

    matches: list[tuple[tuple, dict]] = []
    for p in all_products():
        p_status = (p.get("status") or "").lower()
        if status_q == "in_stock" and p_status != "in_stock":
            continue
        if status_q == "sold" and p_status != "sold":
            continue
        # status_q == "any" → no status filter

        if storage_q:
            p_storage = _normalize(p.get("storage", ""))
            if not p_storage or storage_q not in p_storage:
                continue
        if color_q:
            p_color = _normalize(p.get("color", ""))
            if not p_color or color_q not in p_color:
                continue
        if model_q:
            aliases = _model_aliases(p.get("model", ""))
            if not any(model_q in a or a in model_q for a in aliases):
                continue

        rank = (
            0 if p_status == "in_stock" else 1,
            tuple(-ord(c) for c in (p.get("added_at") or "")),
        )
        matches.append((rank, p))

    matches.sort(key=lambda x: x[0])
    out: list[dict] = []
    keys = (
        "code",
        "model",
        "storage",
        "color",
        "status",
        "price_vnd",
        "battery",
        "warranty_until",
        "added_at",
    )
    for _, p in matches[:max_results]:
        out.append({k: p.get(k) for k in keys if p.get(k) is not None})
    return out


def context_for_llm(*, max_items: int = 30) -> str:
    """Compact human-readable inventory context for LLM prompts.

    Designed to be cheap on tokens (<500 tokens for ~30 phones) while
    still giving the model enough info to answer "do you have …?"
    questions concretely.
    """
    products = in_stock_products()
    if not products:
        return "Kho hiện đang rỗng."
    lines = [f"Kho hiện có {len(products)} máy đang còn hàng:"]
    for p in products[:max_items]:
        price = p.get("price_vnd")
        price_str = f"{price/1_000_000:.1f}tr" if price else "liên hệ"
        bits = [
            p.get("model", ""),
            p.get("storage", ""),
            p.get("color", ""),
        ]
        if p.get("battery"):
            bits.append(f"pin {p['battery']}")
        if p.get("warranty_until"):
            bits.append(f"BH {p['warranty_until'][:10]}")
        bits.append(price_str)
        lines.append(f"- [{p.get('code','?')}] " + " · ".join(b for b in bits if b))
    if len(products) > max_items:
        lines.append(f"… và {len(products) - max_items} máy khác.")
    return "\n".join(lines)
