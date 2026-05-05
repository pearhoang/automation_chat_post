#!/usr/bin/env python3
"""Add a product to the inventory.

Usage::

    add_product.py --model "iPhone 14 Pro Max" --storage 1TB \\
        --color "Titan Gold" --battery 100% --price 22500000 \\
        --warranty-until 2026-10-01 --condition "Cũ - đẹp 99%" \\
        --notes "Đầy đủ phụ kiện zin" \\
        --photos /path/a.jpg /path/b.jpg

The product code is auto-generated as ``{MODEL}-{STORAGE}-{COLOR}-{NNN}``;
pass ``--code`` to override. Photos are copied into the product folder
named ``01.jpg``, ``02.jpg``, … in the order they're listed; the original
files are not modified.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    color_abbr,
    emit_error,
    emit_result,
    inventory_root,
    model_abbr,
    next_code,
    now_iso,
    rebuild_catalog_and_index,
    save_info,
    storage_abbr,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True, help='e.g. "iPhone 14 Pro Max"')
    p.add_argument("--storage", required=True, help='e.g. "1TB"')
    p.add_argument("--color", required=True, help='e.g. "Titan Gold"')
    p.add_argument("--battery", default="", help='e.g. "100%%"')
    p.add_argument(
        "--warranty-until",
        dest="warranty_until",
        default="",
        help="ISO date e.g. 2026-10-01",
    )
    p.add_argument("--price", type=int, default=0, help="Price in VND (integer)")
    p.add_argument("--condition", default="")
    p.add_argument("--notes", default="")
    p.add_argument(
        "--code",
        default=None,
        help="Override auto-generated product code (must be unique).",
    )
    p.add_argument(
        "--photos",
        nargs="*",
        default=[],
        help="Paths to product photos (any order). Copied as 01.jpg, 02.jpg, …",
    )
    p.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Optional tag(s); can be passed multiple times.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    code = args.code or next_code(args.model, args.storage, args.color)
    folder = inventory_root() / "products" / code
    sold_folder = inventory_root() / "sold" / code
    if folder.exists() or sold_folder.exists():
        emit_error(f"product code already exists: {code}")

    folder.mkdir(parents=True)

    # Copy photos as 01.jpg, 02.jpg, ...
    saved_photos: list[str] = []
    for idx, src in enumerate(args.photos, start=1):
        src_path = Path(src).expanduser().resolve()
        if not src_path.exists():
            emit_error(f"photo not found: {src_path}")
        ext = src_path.suffix.lower() or ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".heic"}:
            emit_error(f"unsupported photo extension {ext} for {src_path}")
        dest = folder / f"{idx:02d}{ext}"
        shutil.copyfile(src_path, dest)
        saved_photos.append(dest.name)

    tags = list(args.tag) or [
        model_abbr(args.model).lower(),
        storage_abbr(args.storage).lower(),
        color_abbr(args.color).lower(),
    ]

    info = {
        "code": code,
        "model": args.model,
        "storage": args.storage,
        "color": args.color,
        "condition": args.condition,
        "battery": args.battery,
        "warranty_until": args.warranty_until or None,
        "price_vnd": args.price or None,
        "status": "in_stock",
        "added_at": now_iso(),
        "sold_at": None,
        "notes": args.notes,
        "tags": tags,
        "photo_count": len(saved_photos),
    }
    save_info(folder, info)

    summary = rebuild_catalog_and_index()

    emit_result(
        {
            "ok": True,
            "code": code,
            "folder": str(folder),
            "photos": saved_photos,
            "summary": summary,
        }
    )


if __name__ == "__main__":
    main()
