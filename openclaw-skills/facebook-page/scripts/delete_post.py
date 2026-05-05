#!/usr/bin/env python3
"""Xoá một bài Page (DESTRUCTIVE).

CHỈ chạy sau khi user đã gõ confirmation phrase đầy đủ
"đồng ý xoá bài <full_post_id>". Script này không tự verify
phrase đó — agent gọi script chịu trách nhiệm verify trước.

Facebook có thể trả lỗi nếu app không có quyền `pages_manage_posts`
hoặc post quá cũ; trong trường hợp đó user phải vào Meta Business
Suite → Trash để xoá thủ công.

Output: dòng cuối stdout là JSON với {ok, post_id} hoặc {ok:false, error}.
"""

import argparse
import json
import sys

import requests

from _lib import GRAPH, load_env, require


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-id", required=True, help="Full Facebook post ID")
    args = ap.parse_args()

    env = load_env()
    token = require(env, "page_access_token")

    r = requests.delete(
        f"{GRAPH}/{args.post_id}",
        params={"access_token": token},
        timeout=60,
    )
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"raw": r.text}
        print(
            json.dumps(
                {
                    "ok": False,
                    "post_id": args.post_id,
                    "status": r.status_code,
                    "error": err,
                    "hint": (
                        "Có thể app thiếu permission pages_manage_posts hoặc "
                        "post đã quá cũ. User phải xoá thủ công qua Meta "
                        "Business Suite → Trash trong vòng 30 ngày."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {"ok": True, "post_id": args.post_id, "graph_response": r.json()},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
