"""Per-sender short-term conversation memory for Messenger.

Stored as JSON files at ``/opt/fb-webhook/conversations/{sender}.json``
with the last N user/assistant turns, so the LLM has the context it
needs to answer follow-ups like "cho xem ảnh máy đó đi" referring to a
product mentioned 1-2 messages earlier.

Keep things small: we cap to ``MAX_TURNS`` user+assistant pairs (so
``2 * MAX_TURNS`` messages) and prune anything older. Each turn also
caps the stored text length to avoid runaway disk usage.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

STORE_DIR = Path("/opt/fb-webhook/conversations")
MAX_TURNS = 6           # 6 user + 6 assistant = 12 messages max
MAX_TEXT_LEN = 600      # truncate long inputs (rare on Messenger)

_lock = threading.Lock()


def _safe_id(sender: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", sender)[:64]


def _path_for(sender: str) -> Path:
    return STORE_DIR / f"{_safe_id(sender)}.json"


def load_history(sender: str) -> list[dict]:
    """Return list of role/content dicts (oldest first) for the LLM."""
    p = _path_for(sender)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("conversation: cannot read %s: %s", p, exc)
        return []
    msgs = data.get("messages") or []
    return [
        {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
        for m in msgs
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


def append_turn(sender: str, user_text: str, assistant_text: str) -> None:
    """Append one user+assistant turn and trim to ``MAX_TURNS`` pairs."""
    if not (user_text and assistant_text):
        return
    user_text = user_text[:MAX_TEXT_LEN]
    assistant_text = assistant_text[:MAX_TEXT_LEN]

    p = _path_for(sender)
    with _lock:
        try:
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            existing = load_history(sender)
            existing.append({"role": "user", "content": user_text})
            existing.append({"role": "assistant", "content": assistant_text})
            # cap to MAX_TURNS pairs
            limit = MAX_TURNS * 2
            if len(existing) > limit:
                existing = existing[-limit:]
            p.write_text(
                json.dumps({"messages": existing}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("conversation: cannot write %s: %s", p, exc)
