#!/usr/bin/env python3
"""List products in the inventory.

By default prints in_stock items as JSON. Use ``--all`` to include sold,
``--status reserved`` to filter, or ``--md`` to get a markdown table.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    _index_row,  # type: ignore[reportPrivateUsage]
    all_products,
    emit_result,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--status",
        choices=["in_stock", "reserved", "sold"],
        default=None,
        help="Filter by status (default: in_stock + reserved).",
    )
    p.add_argument("--all", action="store_true", help="Include sold.")
    p.add_argument("--md", action="store_true", help="Print markdown table.")
    args = p.parse_args()

    pool = all_products(include_sold=args.all or args.status == "sold")
    if args.status:
        pool = [p for p in pool if p.get("status") == args.status]
    elif not args.all:
        pool = [p for p in pool if p.get("status") in {"in_stock", "reserved"}]

    if args.md:
        print("| Mã | Model | Storage | Màu | Pin | BH | Giá | Notes |")
        print("|---|---|---|---|---|---|---|---|")
        for p in pool:
            print(_index_row(p))
        return

    emit_result(
        {
            "ok": True,
            "count": len(pool),
            "products": [
                {k: v for k, v in p.items() if not k.startswith("_")} for p in pool
            ],
        }
    )


if __name__ == "__main__":
    main()
