"""Per-sender short-term conversation memory for Messenger.

Stored as JSON files at ``/opt/fb-webhook/conversations/{sender}.json``
with the last N user/assistant turns plus an optional ``post_contexts``
list (set when the conversation was started from a Page-comment private
reply) so the LLM has the context it needs to answer follow-ups like
"cho xem ảnh máy đó đi" — even across the boundary between a comment on
a Page post and the resulting Messenger DM thread.

Keep things small: we cap to ``MAX_TURNS`` user+assistant pairs (so
``2 * MAX_TURNS`` messages) and prune anything older. ``post_contexts``
is preserved across truncation because it carries the original product
the customer was asking about (otherwise the LLM forgets which post the
DM thread came from). We keep up to ``MAX_POST_CONTEXTS`` of them so a
customer who comments on multiple posts doesn't overwrite the older
contexts entirely.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

STORE_DIR = Path("/opt/fb-webhook/conversations")
MAX_TURNS = 12          # 12 user + 12 assistant = 24 messages max
MAX_TEXT_LEN = 800      # truncate long inputs (rare on Messenger)
MAX_POST_CONTEXTS = 3   # remember the 3 most-recent post-comment threads
BURST_WINDOW_SEC = 3.0  # debounce: drop a message if another arrived
                        # from the same sender in the last N seconds

_lock = threading.Lock()
# In-memory burst tracker. Keyed by sender PSID. Cheap and avoids disk
# IO on the hot path; lost on restart, which is fine.
_last_seen: dict[str, float] = {}


def _safe_id(sender: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", sender)[:64]


def _path_for(sender: str) -> Path:
    return STORE_DIR / f"{_safe_id(sender)}.json"


def _read_raw(sender: str) -> dict:
    p = _path_for(sender)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("conversation: cannot read %s: %s", p, exc)
        return {}


def _write_raw(sender: str, data: dict) -> None:
    p = _path_for(sender)
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("conversation: cannot write %s: %s", p, exc)


def load_history(sender: str) -> list[dict]:
    """Return list of role/content dicts (oldest first) for the LLM."""
    data = _read_raw(sender)
    msgs = data.get("messages") or []
    return [
        {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
        for m in msgs
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


def load_post_context(sender: str) -> dict | None:
    """Return the most-recent post-context dict (or None) for this sender.

    The dict has keys ``post_id``, ``post_message``, ``permalink_url``,
    ``comment_text`` (the original comment that triggered the DM).
    """
    contexts = load_post_contexts(sender)
    return contexts[-1] if contexts else None


def load_post_contexts(sender: str) -> list[dict]:
    """Return all stored post contexts for ``sender`` (oldest first).

    Backwards compatible with the older single-dict ``post_context``
    schema: if found, it's wrapped into a single-element list.
    """
    data = _read_raw(sender)
    out: list[dict] = []
    plural = data.get("post_contexts")
    if isinstance(plural, list):
        for pc in plural:
            if isinstance(pc, dict) and (pc.get("post_message") or pc.get("post_id")):
                out.append(pc)
    legacy = data.get("post_context")
    if isinstance(legacy, dict) and (legacy.get("post_message") or legacy.get("post_id")):
        out.append(legacy)
    return out


def should_skip_burst(sender: str) -> bool:
    """Debounce: return True if the sender wrote in the last few seconds.

    We update the timestamp here, so two calls in quick succession yield
    (False, True). The first message is processed normally; immediate
    follow-ups are dropped. Once the customer goes quiet for the burst
    window, the next message gets through again.
    """
    now = time.time()
    last = _last_seen.get(sender, 0.0)
    _last_seen[sender] = now
    return (now - last) < BURST_WINDOW_SEC


def append_turn(sender: str, user_text: str, assistant_text: str) -> None:
    """Append one user+assistant turn and trim to ``MAX_TURNS`` pairs."""
    if not (user_text and assistant_text):
        return
    user_text = user_text[:MAX_TEXT_LEN]
    assistant_text = assistant_text[:MAX_TEXT_LEN]

    with _lock:
        data = _read_raw(sender)
        msgs = data.get("messages") or []
        msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "assistant", "content": assistant_text})
        # cap to MAX_TURNS pairs
        limit = MAX_TURNS * 2
        if len(msgs) > limit:
            msgs = msgs[-limit:]
        data["messages"] = msgs
        _write_raw(sender, data)


def set_post_context(
    sender: str,
    *,
    post_id: str | None,
    post_message: str | None,
    permalink_url: str | None,
    comment_text: str | None,
    private_dm: str | None,
) -> None:
    """Record that this sender's DM thread started from a comment on a post.

    Also seeds the conversation history with a synthetic turn so the LLM
    sees the bot's first DM (which the customer received but which is
    invisible to our regular Messenger webhook because it was sent via
    Private Reply API, not as an inbound message).
    """
    with _lock:
        data = _read_raw(sender)
        # Migrate any legacy single-dict ``post_context`` into the list.
        contexts = list(data.get("post_contexts") or [])
        legacy = data.pop("post_context", None)
        if isinstance(legacy, dict) and not contexts:
            contexts.append(legacy)

        new_pc = {
            k: v
            for k, v in {
                "post_id": post_id,
                "post_message": (post_message or "")[:1500],
                "permalink_url": permalink_url,
                "comment_text": (comment_text or "")[:MAX_TEXT_LEN],
            }.items()
            if v is not None
        }

        # If the same post is already in the list, refresh it in place
        # rather than duplicating; otherwise append.
        replaced = False
        for i, existing in enumerate(contexts):
            if isinstance(existing, dict) and existing.get("post_id") and \
                    existing.get("post_id") == post_id:
                contexts[i] = {**existing, **new_pc}
                replaced = True
                break
        if not replaced:
            contexts.append(new_pc)

        # Cap to the most recent N
        if len(contexts) > MAX_POST_CONTEXTS:
            contexts = contexts[-MAX_POST_CONTEXTS:]
        data["post_contexts"] = contexts

        # Seed the synthetic first turn so LLM history includes the DM
        # we already sent. We frame the "user" side as the original
        # public comment so it reads naturally to the model.
        msgs = data.get("messages") or []
        if private_dm and comment_text:
            seed_user = f"[Khách comment ở bài post] {comment_text[:MAX_TEXT_LEN]}"
            msgs.append({"role": "user", "content": seed_user})
            msgs.append({"role": "assistant", "content": private_dm[:MAX_TEXT_LEN]})
            limit = MAX_TURNS * 2
            if len(msgs) > limit:
                msgs = msgs[-limit:]
            data["messages"] = msgs

        _write_raw(sender, data)
