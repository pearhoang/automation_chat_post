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

from fastapi import FastAPI, HTTPException, Request, Response

from .config import settings
from .fb_client import fb
from .llm import draft_reply
from .telegram import handle_update as tg_handle_update, tg

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

    reply = await draft_reply(text)
    if not reply:
        return

    if settings.human_review_queue:
        # TODO: push to Telegram review bot for approval
        log.info("REVIEW draft for %s :: %s", sender, reply)
        return

    await fb.send_message(sender, reply)


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
    from_id = value.get("from", {}).get("id")
    if from_id == settings.fb_page_id:
        return  # ignore our own comments
    if not (comment_id and text):
        return

    log.info("COMMENT id=%s text=%s", comment_id, text[:80])

    if not settings.auto_reply_enabled:
        return
    reply = await draft_reply(text, context="The user is commenting on a Page post.")
    if not reply:
        return
    if settings.human_review_queue:
        log.info("REVIEW comment %s :: %s", comment_id, reply)
        return
    await fb.reply_comment(comment_id, reply)


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


@app.on_event("shutdown")
async def _shutdown() -> None:
    await fb.aclose()
    await tg.aclose()
