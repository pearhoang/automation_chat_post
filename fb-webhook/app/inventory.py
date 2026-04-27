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
