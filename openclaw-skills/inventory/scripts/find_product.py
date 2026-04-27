#!/usr/bin/env python3
"""Free-text search over the inventory.

The query is normalised (lowercased, accents stripped) and matched against
``code``, ``model``, ``storage``, ``color``, ``tags``, ``notes``. Tokens
are AND'd: every space-separated token must appear in at least one field.

By default only ``in_stock`` items are returned; pass ``--all`` to include
``reserved`` and ``sold``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    _strip_accents,  # type: ignore[reportPrivateUsage]
    all_products,
    emit_result,
)


def normalise(text: str) -> str:
    return _strip_accents(text or "").lower()


def matches(info: dict, tokens: list[str]) -> bool:
    haystack = " ".join(
        [
            info.get("code", ""),
            info.get("model", ""),
            info.get("storage", ""),
            info.get("color", ""),
            info.get("notes", "") or "",
            info.get("condition", "") or "",
            " ".join(info.get("tags", []) or []),
        ]
    )
    haystack = normalise(haystack)
    return all(tok in haystack for tok in tokens)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("query", nargs="+", help="Free-text query (Vietnamese OK).")
    p.add_argument(
        "--all",
        action="store_true",
        help="Include reserved + sold (default: in_stock only).",
    )
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    tokens = [t for t in normalise(" ".join(args.query)).split() if t]
    if not tokens:
        emit_result({"ok": True, "query": " ".join(args.query), "results": []})
        return

    pool = all_products(include_sold=args.all)
    if not args.all:
        pool = [p for p in pool if p.get("status") == "in_stock"]

    hits = [p for p in pool if matches(p, tokens)][: args.limit]
    emit_result(
        {
            "ok": True,
            "query": " ".join(args.query),
            "count": len(hits),
            "results": [
                {k: v for k, v in p.items() if not k.startswith("_")} for p in hits
            ],
        }
    )


if __name__ == "__main__":
    main()
