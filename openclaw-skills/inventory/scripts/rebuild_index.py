#!/usr/bin/env python3
"""Regenerate ``catalog.json`` and ``INDEX.md`` from product folders.

Run this if you've edited an ``info.json`` by hand or moved folders
manually.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import emit_result, rebuild_catalog_and_index  # noqa: E402


def main() -> None:
    summary = rebuild_catalog_and_index()
    emit_result({"ok": True, "summary": summary})


if __name__ == "__main__":
    main()
