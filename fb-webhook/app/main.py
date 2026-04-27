"""FB Webhook entry point.

Endpoints:
    GET  /healthz                — liveness
    GET  /webhook/messenger      — verification handshake (hub.challenge)
    POST /webhook/messenger      — incoming Messenger / page-feed events

Run with:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

import hashlib
import hmac
import json
import logging
import re

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from .config import settings
from .conversation import append_turn, load_history
from .fb_client import fb
from .inventory import (
    context_for_llm as inventory_context,
    find_by_code,
    product_photos,
)
from .llm import classify_comment, draft_reply
from .telegram import handle_update as tg_handle_update, tg

# LLM may emit "[SEND_PHOTOS:CODE]<text>" to ask the webhook to attach
# photos for that product before sending the text. The token is consumed
# by the webhook and never forwarded to the customer.
_SEND_PHOTOS_RE = re.compile(r"^\s*\[SEND_PHOTOS:\s*([A-Z0-9_-]+)\s*\]\s*", re.I)

# Comment replies may emit "[PRIVATE_REPLY]<dm body>\n<public reply>" so we
# can both DM the commenter (Private Reply) AND post the public response.
# The LLM is expected to put the private message FIRST (a single line, no
# newlines) followed by the public-comment body.
_PRIVATE_REPLY_RE = re.compile(
    r"^\s*\[PRIVATE_REPLY\]\s*(.+?)\s*(?:\n+(.+))?\Z", re.S | re.I
)

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("fb-webhook")

app = FastAPI(title="Demo Shop FB Webhook", version="0.1.0")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "auto_reply": settings.auto_reply_enabled}


# ---------------------------------------------------------------------------
# Webhook verification (called once when you set the URL in FB App settings)
# ---------------------------------------------------------------------------
@app.get("/webhook/messenger")
async def verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == settings.fb_verify_token:
        log.info("Webhook verified by Facebook")
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verify_token mismatch")


# ---------------------------------------------------------------------------
# Webhook events (Messenger messages, page comments, …)
# ---------------------------------------------------------------------------
def _verify_signature(raw: bytes, header_value: str) -> bool:
    """Validate X-Hub-Signature-256 sent by Facebook."""
    if not settings.fb_app_secret or not header_value:
        return False
    if not header_value.startswith("sha256="):
        return False
    sent = header_value.split("=", 1)[1]
    digest = hmac.new(
        settings.fb_app_secret.encode("utf-8"),
        raw,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sent, digest)


@app.post("/webhook/messenger")
async def webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    if settings.fb_app_secret and not _verify_signature(raw, sig):
        log.warning("Invalid signature, dropping event")
        raise HTTPException(status_code=401, detail="bad signature")

    body = json.loads(raw)
    object_type = body.get("object")
    entries = body.get("entry", [])

    for entry in entries:
        # ---- Messenger messages ----
        for evt in entry.get("messaging", []) or []:
            await _handle_messenger_event(evt)

        # ---- Page feed (post comments) ----
        for change in entry.get("changes", []) or []:
            await _handle_page_change(change)

    log.debug("processed %s entries (object=%s)", len(entries), object_type)
    # FB requires 200 OK within 20s or it retries
    return {"received": True}


# ---------------------------------------------------------------------------
# Handlers (skeleton — add real product lookup / RAG here)
# ---------------------------------------------------------------------------
async def _handle_messenger_event(evt: dict) -> None:
    sender = evt.get("sender", {}).get("id")
    msg = evt.get("message", {}) or {}
    text = msg.get("text")
    if not (sender and text):
        return

    log.info("MSG from=%s text=%s", sender, text[:80])

    if not settings.auto_reply_enabled:
        log.info("auto_reply disabled — would draft reply only")
        return

    history = load_history(sender)
    reply = await draft_reply(
        text,
        context=inventory_context(),
        history=history,
    )
    if not reply:
        return

    if settings.human_review_queue:
        # TODO: push to Telegram review bot for approval
        log.info("REVIEW draft for %s :: %s", sender, reply)
        return

    photo_code, text_reply = _extract_photo_directive(reply)
    if photo_code:
        await _send_product_photos(sender, photo_code)
    if text_reply:
        await fb.send_message(sender, text_reply)
    # remember the *clean* reply (without the [SEND_PHOTOS] token) so the
    # next turn the LLM can refer back to it by code.
    append_turn(sender, text, text_reply or reply)


def _extract_photo_directive(reply: str) -> tuple[str | None, str]:
    """Parse a leading ``[SEND_PHOTOS:CODE]`` token. Returns (code, rest)."""
    m = _SEND_PHOTOS_RE.match(reply)
    if not m:
        return None, reply
    return m.group(1).upper(), _SEND_PHOTOS_RE.sub("", reply, count=1).strip()


async def _send_product_photos(sender: str, code: str) -> None:
    """Look up product by code and send up to 3 photos via Send API."""
    product = find_by_code(code)
    if not product:
        log.warning("send_photos: unknown product code=%s", code)
        return
    photos = product_photos(code, max_photos=3)
    if not photos:
        log.warning("send_photos: no photo files for code=%s", code)
        return
    log.info("send_photos: code=%s n=%d", code, len(photos))
    for path in photos:
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.warning("send_photos: cannot read %s: %s", path, exc)
            continue
        try:
            await fb.send_image(sender, data, filename=path.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("send_photos: failed for %s: %s", path.name, exc)


def _split_labels(raw: str) -> set[str]:
    """Parse a comma-separated label list from .env into a normalized set."""
    return {x.strip().lower() for x in (raw or "").split(",") if x.strip()}


async def _forward_comment_to_admin(
    *,
    label: str,
    confidence: float,
    reason: str,
    text: str,
    comment_id: str,
    post_id: str | None,
    from_name: str | None,
    hidden: bool,
) -> None:
    """Push a moderation alert to the Telegram admin (best-effort)."""
    if not settings.telegram_bot_token or not settings.telegram_admin_chat_id:
        return
    flag = "🚫 ĐÃ ẨN" if hidden else "⚠️ CẦN XEM"
    parts = [
        f"{flag} comment ({label}, {int(confidence * 100)}%)",
        f"từ: {from_name or '?'}",
    ]
    if post_id:
        parts.append(f"post: https://www.facebook.com/{post_id}")
    parts.append(f"id: {comment_id}")
    if reason:
        parts.append(f"lý do: {reason}")
    parts.append("")
    parts.append(text[:600])
    try:
        await tg.send_message(settings.telegram_admin_chat_id, "\n".join(parts))
    except Exception:  # noqa: BLE001
        log.exception("forward to admin failed")


async def _handle_page_change(change: dict) -> None:
    field = change.get("field")
    value = change.get("value", {})
    if field != "feed":
        return
    if value.get("item") != "comment":
        return
    if value.get("verb") not in ("add", "edited"):
        return
    comment_id = value.get("comment_id")
    text = value.get("message")
    from_obj = value.get("from", {}) or {}
    from_id = from_obj.get("id")
    from_name = from_obj.get("name")
    post_id = value.get("post_id")
    if from_id == settings.fb_page_id:
        return  # ignore our own comments
    if not (comment_id and text):
        return

    log.info("COMMENT id=%s text=%s", comment_id, text[:80])

    classification = await classify_comment(text)
    label = classification["label"]
    confidence = classification["confidence"]
    reason = classification["reason"]
    log.info(
        "COMMENT classified id=%s label=%s confidence=%.2f reason=%s",
        comment_id,
        label,
        confidence,
        reason[:80],
    )

    hide_labels = _split_labels(settings.comment_auto_hide_labels)
    forward_labels = _split_labels(settings.comment_forward_labels)
    skip_reply_labels = _split_labels(settings.comment_skip_reply_labels)
    min_conf = settings.comment_action_min_confidence

    # Auto-hide flagged comments first so they disappear from public view
    # before we even draft a reply. We only hide when the classifier is
    # confident — low-confidence calls fall through to normal handling.
    hidden = False
    if label in hide_labels and confidence >= min_conf:
        try:
            await fb.hide_comment(comment_id)
            hidden = True
            log.info("COMMENT hidden id=%s label=%s", comment_id, label)
        except Exception:  # noqa: BLE001
            log.exception("hide_comment failed id=%s", comment_id)

    # Forward sensitive labels to the admin so a human can step in.
    if label in forward_labels:
        await _forward_comment_to_admin(
            label=label,
            confidence=confidence,
            reason=reason,
            text=text,
            comment_id=comment_id,
            post_id=post_id,
            from_name=from_name,
            hidden=hidden,
        )

    if not settings.auto_reply_enabled:
        return
    if label in skip_reply_labels and confidence >= min_conf:
        log.info("COMMENT skip_reply id=%s label=%s", comment_id, label)
        return

    # Pull the post body so the LLM can reference what the customer is
    # actually looking at — e.g. "iPhone 13 Pro Max 256GB Sierra Blue,
    # 15.5tr". Best-effort: if the API call fails we still reply.
    post_block = ""
    if post_id:
        try:
            post = await fb.fetch_post(post_id)
            post_msg = (post.get("message") or "").strip()
            permalink = post.get("permalink_url") or ""
            if post_msg or permalink:
                post_block = (
                    "Bài post liên quan:\n"
                    + (f"  link: {permalink}\n" if permalink else "")
                    + (f"  nội dung: {post_msg}\n" if post_msg else "")
                )
        except Exception:  # noqa: BLE001
            log.exception("fetch_post failed post_id=%s", post_id)

    ctx_parts = [
        "Khách đang comment vào 1 bài post của Page.",
        f"(Internal classifier: label={label}, confidence={confidence:.2f}.)",
    ]
    if post_block:
        ctx_parts.append(post_block)
    ctx_parts.append(inventory_context())

    reply = await draft_reply(text, context="\n\n".join(ctx_parts))
    if not reply:
        return
    if settings.human_review_queue:
        log.info("REVIEW comment %s :: %s", comment_id, reply)
        return

    private_msg, public_msg = _extract_private_reply(reply)
    if private_msg:
        try:
            await fb.private_reply_to_comment(comment_id, private_msg)
            log.info("COMMENT private_reply id=%s", comment_id)
        except Exception:  # noqa: BLE001
            log.exception("private_reply_to_comment failed id=%s", comment_id)
    if public_msg:
        await fb.reply_comment(comment_id, public_msg)


def _extract_private_reply(reply: str) -> tuple[str | None, str]:
    """Parse a leading ``[PRIVATE_REPLY]<dm>\\n<public>`` token.

    Returns ``(private_msg, public_msg)``. If the token is absent the
    whole reply is treated as the public-comment body.
    """
    m = _PRIVATE_REPLY_RE.match(reply)
    if not m:
        return None, reply.strip()
    private_msg = m.group(1).strip()
    public_msg = (m.group(2) or "").strip()
    if not public_msg:
        # LLM forgot to add a public reply — fall back to a generic
        # placeholder so the post still has a public response.
        public_msg = "ib bạn nha"
    return private_msg, public_msg


# ---------------------------------------------------------------------------
# Telegram admin bot webhook
# ---------------------------------------------------------------------------
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=404, detail="telegram disabled")

    # Telegram echoes back the secret token we registered with setWebhook.
    # We refuse the request unless it matches — anyone can guess our URL.
    if settings.telegram_webhook_secret:
        sent = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(sent, settings.telegram_webhook_secret):
            log.warning("telegram bad secret_token (ignored)")
            raise HTTPException(status_code=401, detail="bad secret_token")

    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json") from None

    try:
        await tg_handle_update(update)
    except Exception:
        log.exception("tg_handle_update crashed")
    # Always 200 OK so Telegram doesn't keep retrying a poisoned update.
    return {"ok": True}


# ---------------------------------------------------------------------------
# LLM pass-through proxy
#
# OpenClaw normalizes model ids to `<provider>/<id-after-first-slash>` and
# sends only the trailing `<id>` in the request body. 9Router however
# requires the *full* `cx/gpt-5.5` (provider prefix included) — sending
# bare `gpt-5.5` returns 403 "model_not_found".
#
# This proxy sits in front of 9Router (or any OpenAI-compatible gateway)
# and re-prefixes the model name on the way out so OpenClaw's catalog can
# stay registered with `id: gpt-5.5` (no slash → no surprise normalization).
#
# Point OpenClaw's models.providers.<x>.baseUrl at
# `https://api.jazzrelaxation.com/llm-proxy/v1` instead of the upstream URL.
# ---------------------------------------------------------------------------
_LLM_FORWARD_HEADERS = {
    "authorization",
    "content-type",
    "accept",
    # Deliberately NOT forwarding accept-encoding — we want upstream to
    # send plain text so we can re-emit it without re-encoding.
    # Deliberately NOT forwarding user-agent — 9Router blocks the
    # `OpenAI/JS` user-agent that OpenClaw's embedded client sends.
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-package-version",
    "x-stainless-lang",
}


@app.api_route(
    "/llm-proxy/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
async def llm_proxy(path: str, request: Request):
    """Forward to settings.openai_base_url, prepending `cx/` to model names."""

    upstream = f"{settings.openai_base_url.rstrip('/')}/{path}"
    body = await request.body()

    # Try to rewrite the model field for chat/completions and similar.
    # (No-op if body isn't JSON or has no model.)
    original_model: str | None = None
    if body and request.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, str) and model:
                original_model = model
                # Normalize OpenClaw style "openai/gpt-5.5" or bare "gpt-5.5"
                # into the 9Router-required "cx/<id>" form.
                bare = model.split("/", 1)[1] if "/" in model else model
                payload["model"] = f"cx/{bare}"
                body = json.dumps(payload).encode()

    headers = {
        k: v for k, v in request.headers.items() if k.lower() in _LLM_FORWARD_HEADERS
    }
    # Override user-agent: 9Router rejects the upstream OpenAI/JS SDK UA with
    # 403 "Your request was blocked." Use a neutral curl-ish UA.
    headers["user-agent"] = "fb-webhook-llm-proxy/1.0"

    async with httpx.AsyncClient(timeout=120.0) as http:
        try:
            r = await http.request(
                request.method,
                upstream,
                content=body,
                headers=headers,
                params=dict(request.query_params),
            )
        except httpx.HTTPError as exc:
            log.error("llm-proxy upstream error: %s", exc)
            raise HTTPException(status_code=502, detail="upstream_error") from exc

    if r.status_code >= 400:
        log.warning(
            "llm-proxy upstream %s for model=%s -> %s body=%s",
            r.status_code,
            original_model,
            upstream,
            r.text[:300],
        )

    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    await fb.aclose()
    await tg.aclose()
