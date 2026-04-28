#!/usr/bin/env python3
"""Đăng bài lên Facebook Page.

Có 3 mode tuỳ vào số lượng ảnh:
- 0 ảnh → POST {page_id}/feed với `message`
- 1 ảnh → POST {page_id}/photos với `caption` + `source`
- 2+ ảnh → upload từng ảnh `published=false` → POST {page_id}/feed với attached_media

Output: dòng cuối stdout là JSON một dòng có {ok, post_id, post_url, photo_ids}.
"""

import argparse
import json
import sys
from pathlib import Path

import requests

from _lib import GRAPH, load_env, require


def upload_photo_unpublished(page_id: str, token: str, photo: Path) -> str:
    with photo.open("rb") as fh:
        r = requests.post(
            f"{GRAPH}/{page_id}/photos",
            data={"published": "false", "access_token": token},
            files={"source": (photo.name, fh)},
            timeout=120,
        )
    r.raise_for_status()
    pid = r.json().get("id")
    if not pid:
        raise RuntimeError(f"upload failed (no id): {r.text}")
    return pid


def post_with_attached_media(
    page_id: str, token: str, message: str, photo_ids: list[str]
) -> dict:
    payload: dict[str, str] = {"message": message, "access_token": token}
    for i, pid in enumerate(photo_ids):
        payload[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid})
    r = requests.post(f"{GRAPH}/{page_id}/feed", data=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def post_single_photo(
    page_id: str, token: str, message: str, photo: Path
) -> dict:
    with photo.open("rb") as fh:
        r = requests.post(
            f"{GRAPH}/{page_id}/photos",
            data={"caption": message, "access_token": token},
            files={"source": (photo.name, fh)},
            timeout=120,
        )
    r.raise_for_status()
    return r.json()


def post_text_only(page_id: str, token: str, message: str) -> dict:
    r = requests.post(
        f"{GRAPH}/{page_id}/feed",
        data={"message": message, "access_token": token},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", required=True, help="Nội dung bài đăng")
    ap.add_argument(
        "--photos", nargs="*", default=[], help="Đường dẫn tuyệt đối tới ảnh"
    )
    args = ap.parse_args()

    env = load_env()
    page_id = require(env, "page_id")
    token = require(env, "page_access_token")
    page_name = env.get("page_name", "")

    photos = [Path(p) for p in args.photos]
    for p in photos:
        if not p.exists():
            print(f"missing photo: {p}", file=sys.stderr)
            return 2

    photo_ids: list[str] = []
    if not photos:
        result = post_text_only(page_id, token, args.message)
    elif len(photos) == 1:
        result = post_single_photo(page_id, token, args.message, photos[0])
    else:
        photo_ids = [upload_photo_unpublished(page_id, token, p) for p in photos]
        result = post_with_attached_media(page_id, token, args.message, photo_ids)

    post_id = result.get("post_id") or result.get("id")
    out = {
        "ok": True,
        "post_id": post_id,
        "post_url": f"https://www.facebook.com/{post_id}" if post_id else None,
        "photo_count": len(photos),
        "photo_ids": photo_ids,
        "page_name": page_name,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
