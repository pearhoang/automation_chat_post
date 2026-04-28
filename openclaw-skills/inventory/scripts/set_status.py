#!/usr/bin/env python3
"""Update a product's status (``in_stock`` / ``reserved``).

This script does NOT handle ``sold`` — use ``mark_sold.py`` instead so the
folder also gets archived.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    emit_error,
    emit_result,
    inventory_root,
    load_info,
    rebuild_catalog_and_index,
    save_info,
)

ALLOWED = {"in_stock", "reserved"}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--code", required=True)
    p.add_argument("--status", required=True, choices=sorted(ALLOWED))
    p.add_argument("--note", default="")
    args = p.parse_args()

    folder = inventory_root() / "products" / args.code
    if not folder.exists():
        emit_error(f"product not found in stock: {args.code}")

    info = load_info(folder)
    info["status"] = args.status
    if args.note:
        existing = info.get("notes") or ""
        info["notes"] = (existing + " | " + args.note).strip(" |")
    save_info(folder, info)

    summary = rebuild_catalog_and_index()
    emit_result(
        {"ok": True, "code": args.code, "status": args.status, "summary": summary}
    )


if __name__ == "__main__":
    main()
