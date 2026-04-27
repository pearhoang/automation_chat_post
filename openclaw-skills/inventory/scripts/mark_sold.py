#!/usr/bin/env python3
"""Move a product from ``products/`` to ``sold/`` and stamp ``sold_at``."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    emit_error,
    emit_result,
    inventory_root,
    load_info,
    now_iso,
    rebuild_catalog_and_index,
    save_info,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--code", required=True)
    p.add_argument("--note", default="", help="Optional note to append.")
    args = p.parse_args()

    src = inventory_root() / "products" / args.code
    dst = inventory_root() / "sold" / args.code

    if not src.exists():
        if dst.exists():
            emit_error(f"already sold: {args.code}")
        emit_error(f"product not found: {args.code}")

    info = load_info(src)
    info["status"] = "sold"
    info["sold_at"] = now_iso()
    if args.note:
        existing = info.get("notes") or ""
        info["notes"] = (existing + " | " + args.note).strip(" |")
    save_info(src, info)

    shutil.move(str(src), str(dst))

    summary = rebuild_catalog_and_index()
    emit_result({"ok": True, "code": args.code, "folder": str(dst), "summary": summary})


if __name__ == "__main__":
    main()
