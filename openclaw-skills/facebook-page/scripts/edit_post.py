#!/usr/bin/env python3
"""Sửa text của một bài đã đăng trên Page.

Chạy `POST /{post_id}` với field `message`. Facebook cho phép edit text
của bài đã đăng (chỉ KHÔNG cho thay ảnh / attached_media — nếu cần thay
ảnh thì phải xoá bài cũ rồi đăng bài mới).

Output: dòng cuối stdout là JSON với {ok, post_id, message}.
"""

import argparse
import json
import sys

import requests

from _lib import GRAPH, load_env, require


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--post-id",
        required=True,
        help="Full Facebook post ID (vd 1065817536621033_122xxxx)",
    )
    ap.add_argument("--message", required=True, help="Nội dung mới")
    args = ap.parse_args()

    env = load_env()
    token = require(env, "page_access_token")

    r = requests.post(
        f"{GRAPH}/{args.post_id}",
        data={"message": args.message, "access_token": token},
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
                },
                ensure_ascii=False,
            )
        )
        return 1

    out = {
        "ok": True,
        "post_id": args.post_id,
        "post_url": f"https://www.facebook.com/{args.post_id}",
        "message": args.message,
        "graph_response": r.json(),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
