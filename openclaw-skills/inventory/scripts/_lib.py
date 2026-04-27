"""Shared helpers for the inventory skill scripts.

Resolves the inventory root from the OPENCLAW_WORKSPACE env var (default
``$HOME/.openclaw/workspace``) and exposes catalog/index regeneration
plus product-code generation helpers.

All scripts in this directory keep the same conventions:
- Operate on filesystem only — no remote calls.
- Always rebuild ``catalog.json`` + ``INDEX.md`` after mutations so
  fb-webhook + the agent see consistent state.
- Print machine-readable JSON on the final stdout line so the agent can
  parse the result of a tool call. Human prose goes on stderr.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def workspace_root() -> Path:
    """Return ``$OPENCLAW_WORKSPACE`` or ``$HOME/.openclaw/workspace``."""
    env = os.environ.get("OPENCLAW_WORKSPACE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".openclaw" / "workspace"


def inventory_root() -> Path:
    p = workspace_root() / "inventory"
    p.mkdir(parents=True, exist_ok=True)
    (p / "products").mkdir(exist_ok=True)
    (p / "sold").mkdir(exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------
_MODEL_ABBR = [
    # order matters: longest match first
    (re.compile(r"iphone\s*15\s*pro\s*max", re.I), "IP15PM"),
    (re.compile(r"iphone\s*15\s*pro", re.I), "IP15PRO"),
    (re.compile(r"iphone\s*15\s*plus", re.I), "IP15PLUS"),
    (re.compile(r"iphone\s*15", re.I), "IP15"),
    (re.compile(r"iphone\s*14\s*pro\s*max", re.I), "IP14PM"),
    (re.compile(r"iphone\s*14\s*pro", re.I), "IP14PRO"),
    (re.compile(r"iphone\s*14\s*plus", re.I), "IP14PLUS"),
    (re.compile(r"iphone\s*14", re.I), "IP14"),
    (re.compile(r"iphone\s*13\s*pro\s*max", re.I), "IP13PM"),
    (re.compile(r"iphone\s*13\s*pro", re.I), "IP13PRO"),
    (re.compile(r"iphone\s*13\s*mini", re.I), "IP13MINI"),
    (re.compile(r"iphone\s*13", re.I), "IP13"),
    (re.compile(r"iphone\s*12\s*pro\s*max", re.I), "IP12PM"),
    (re.compile(r"iphone\s*12\s*pro", re.I), "IP12PRO"),
    (re.compile(r"iphone\s*12\s*mini", re.I), "IP12MINI"),
    (re.compile(r"iphone\s*12", re.I), "IP12"),
    (re.compile(r"iphone\s*11\s*pro\s*max", re.I), "IP11PM"),
    (re.compile(r"iphone\s*11\s*pro", re.I), "IP11PRO"),
    (re.compile(r"iphone\s*11", re.I), "IP11"),
    (re.compile(r"iphone\s*xs\s*max", re.I), "IPXSMAX"),
    (re.compile(r"iphone\s*xs", re.I), "IPXS"),
    (re.compile(r"iphone\s*xr", re.I), "IPXR"),
    (re.compile(r"iphone\s*x\b", re.I), "IPX"),
    (re.compile(r"iphone\s*16\s*pro\s*max", re.I), "IP16PM"),
    (re.compile(r"iphone\s*16\s*pro", re.I), "IP16PRO"),
    (re.compile(r"iphone\s*16\s*plus", re.I), "IP16PLUS"),
    (re.compile(r"iphone\s*16", re.I), "IP16"),
    (re.compile(r"ipad\s*pro", re.I), "IPADPRO"),
    (re.compile(r"ipad\s*air", re.I), "IPADAIR"),
    (re.compile(r"ipad\s*mini", re.I), "IPADMINI"),
    (re.compile(r"ipad", re.I), "IPAD"),
    (re.compile(r"macbook\s*pro", re.I), "MBPRO"),
    (re.compile(r"macbook\s*air", re.I), "MBAIR"),
    (re.compile(r"macbook", re.I), "MB"),
    (re.compile(r"apple\s*watch", re.I), "AW"),
    (re.compile(r"airpods\s*pro", re.I), "APP"),
    (re.compile(r"airpods\s*max", re.I), "APMAX"),
    (re.compile(r"airpods", re.I), "AP"),
]

_COLOR_ABBR = [
    (re.compile(r"titan\s*tự\s*nhiên|natural\s*titanium", re.I), "TITNAT"),
    (re.compile(r"titan\s*xanh|blue\s*titanium", re.I), "TITBLU"),
    (re.compile(r"titan\s*trắng|white\s*titanium", re.I), "TITWHT"),
    (re.compile(r"titan\s*đen|black\s*titanium", re.I), "TITBLK"),
    (re.compile(r"titan\s*sa\s*mạc|desert\s*titanium", re.I), "TITDES"),
    (re.compile(r"titan\s*gold|titan\s*vàng|gold\s*titanium", re.I), "TIT"),
    (re.compile(r"titan", re.I), "TIT"),
    (re.compile(r"tím", re.I), "PUR"),
    (re.compile(r"vàng|gold", re.I), "GLD"),
    (re.compile(r"đen|black", re.I), "BLK"),
    (re.compile(r"trắng|white", re.I), "WHT"),
    (re.compile(r"xanh\s*lá|green", re.I), "GRN"),
    (re.compile(r"xanh\s*dương|blue", re.I), "BLU"),
    (re.compile(r"xanh", re.I), "BLU"),
    (re.compile(r"đỏ|red", re.I), "RED"),
    (re.compile(r"hồng|pink", re.I), "PNK"),
    (re.compile(r"bạc|silver", re.I), "SLV"),
    (re.compile(r"xám|gray|grey", re.I), "GRY"),
]


def model_abbr(model: str) -> str:
    for pat, abbr in _MODEL_ABBR:
        if pat.search(model):
            return abbr
    # fallback: strip non-alphanum, uppercase, take first 8 chars
    cleaned = re.sub(r"[^A-Za-z0-9]", "", _strip_accents(model)).upper()
    return cleaned[:8] or "ITEM"


def color_abbr(color: str) -> str:
    if not color:
        return "STD"
    for pat, abbr in _COLOR_ABBR:
        if pat.search(color):
            return abbr
    cleaned = re.sub(r"[^A-Za-z]", "", _strip_accents(color)).upper()
    return cleaned[:3] or "STD"


def storage_abbr(storage: str) -> str:
    if not storage:
        return "NA"
    s = re.sub(r"\s+", "", _strip_accents(storage)).upper()
    # normalize "1024GB" -> "1TB", "1tb" -> "1TB"
    s = re.sub(r"1024GB", "1TB", s)
    return s


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(ch) != "Mn"
    )


def next_code(model: str, storage: str, color: str) -> str:
    """Return next available product code for this combination.

    Walks both ``products/`` and ``sold/`` so codes never collide even after
    items are sold.
    """
    base = f"{model_abbr(model)}-{storage_abbr(storage)}-{color_abbr(color)}"
    used: set[int] = set()
    for parent in (inventory_root() / "products", inventory_root() / "sold"):
        for entry in parent.iterdir() if parent.exists() else []:
            if not entry.is_dir():
                continue
            m = re.match(rf"^{re.escape(base)}-(\d+)$", entry.name)
            if m:
                used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"{base}-{n:03d}"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_info(folder: Path) -> dict:
    return json.loads((folder / "info.json").read_text(encoding="utf-8"))


def save_info(folder: Path, info: dict) -> None:
    (folder / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def all_products(include_sold: bool = False) -> list[dict]:
    out: list[dict] = []
    roots = [inventory_root() / "products"]
    if include_sold:
        roots.append(inventory_root() / "sold")
    for parent in roots:
        if not parent.exists():
            continue
        for folder in sorted(parent.iterdir()):
            if not folder.is_dir():
                continue
            info_path = folder / "info.json"
            if not info_path.exists():
                continue
            try:
                info = load_info(folder)
            except json.JSONDecodeError:
                continue
            info["_folder"] = str(folder)
            out.append(info)
    return out


# ---------------------------------------------------------------------------
# Index regeneration
# ---------------------------------------------------------------------------
def rebuild_catalog_and_index() -> dict:
    """Regenerate ``catalog.json`` (in-stock + reserved only) and INDEX.md.

    Returns a small summary dict suitable for printing back to the agent.
    """
    products = all_products(include_sold=True)
    in_stock = [p for p in products if p.get("status") == "in_stock"]
    reserved = [p for p in products if p.get("status") == "reserved"]
    sold = [p for p in products if p.get("status") == "sold"]

    catalog = {
        "version": 1,
        "generated_at": now_iso(),
        "summary": {
            "in_stock": len(in_stock),
            "reserved": len(reserved),
            "sold": len(sold),
        },
        "products": [
            _public_product(p) for p in (in_stock + reserved)
        ],
    }
    (inventory_root() / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    index_md = _build_index_md(in_stock, reserved, sold)
    (inventory_root() / "INDEX.md").write_text(index_md, encoding="utf-8")

    return catalog["summary"]


def _public_product(info: dict) -> dict:
    """Strip internal-only fields before publishing into catalog.json."""
    return {
        k: v
        for k, v in info.items()
        if not k.startswith("_")
    }


def _build_index_md(
    in_stock: list[dict], reserved: list[dict], sold: list[dict]
) -> str:
    lines: list[str] = []
    lines.append("# Inventory INDEX")
    lines.append("")
    lines.append(
        f"_Generated {now_iso()} — "
        f"{len(in_stock)} còn hàng, {len(reserved)} đã đặt cọc, "
        f"{len(sold)} đã bán._"
    )
    lines.append("")
    lines.append("## Đang còn hàng (in_stock)")
    if not in_stock:
        lines.append("")
        lines.append("_(kho rỗng)_")
    else:
        lines.append("")
        lines.append("| Mã | Model | Storage | Màu | Pin | BH | Giá | Notes |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for p in in_stock:
            lines.append(_index_row(p))
    lines.append("")
    lines.append("## Đặt cọc (reserved)")
    if not reserved:
        lines.append("")
        lines.append("_(không có)_")
    else:
        lines.append("")
        lines.append("| Mã | Model | Storage | Màu | Notes |")
        lines.append("|---|---|---|---|---|")
        for p in reserved:
            lines.append(
                f"| `{p['code']}` | {p.get('model','')} | {p.get('storage','')} "
                f"| {p.get('color','')} | {p.get('notes','')} |"
            )
    lines.append("")
    lines.append("## Đã bán (sold) — 10 gần nhất")
    if not sold:
        lines.append("")
        lines.append("_(chưa có)_")
    else:
        recent = sorted(sold, key=lambda p: p.get("sold_at") or "", reverse=True)[:10]
        lines.append("")
        lines.append("| Mã | Model | Bán ngày |")
        lines.append("|---|---|---|")
        for p in recent:
            lines.append(
                f"| `{p['code']}` | {p.get('model','')} | "
                f"{(p.get('sold_at') or '')[:10]} |"
            )
    lines.append("")
    return "\n".join(lines)


def _index_row(p: dict) -> str:
    price = p.get("price_vnd")
    price_str = f"{price/1_000_000:.1f}tr" if price else "—"
    return (
        f"| `{p['code']}` | {p.get('model','')} | {p.get('storage','')} "
        f"| {p.get('color','')} | {p.get('battery','')} "
        f"| {(p.get('warranty_until') or '')[:10]} | {price_str} "
        f"| {(p.get('notes') or '')[:60]} |"
    )


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def emit_result(payload: dict) -> None:
    """Print a JSON result line to stdout for the agent to parse."""
    print(json.dumps(payload, ensure_ascii=False))


def emit_error(msg: str, *, exit_code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(exit_code)
