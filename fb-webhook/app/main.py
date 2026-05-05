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
from .conversation import (
    append_turn,
    load_history,
    load_post_context,
    set_post_context,
    should_skip_burst,
)
from .fb_client import fb
from .inventory import (
    context_for_llm as inventory_context,
    find_by_code,
    find_products_in_text,
    lookup_products,
    match_post_to_product,
    product_photos,
)
from .llm import classify_comment, detect_intent, detect_phone, draft_reply
from .shop_info import shop_info_block
from .telegram import handle_update as tg_handle_update, notify_admin, tg

# LLM may emit "[SEND_PHOTOS:CODE]<text>" to ask the webhook to attach
# photos for that product before sending the text. The token is consumed
# by the webhook and never forwarded to the customer.
_SEND_PHOTOS_RE = re.compile(r"^\s*\[SEND_PHOTOS:\s*([A-Z0-9_-]+)\s*\]\s*", re.I)

# Heuristic: customer is asking us to send (or re-send) a product photo.
# Used as a fallback when the LLM forgets to emit [SEND_PHOTOS:CODE]
# but the post-context already pinned a specific product. We err on
# the inclusive side — a missing photo is more annoying than an extra
# one and the photo-send only fires when we have a concrete product.
#
# We match against the diacritic-stripped text so customers writing
# "anh that" or "ảnh thật" both hit. Patterns are written in the
# stripped form (no Vietnamese tone marks).
_PHOTO_REQUEST_RE = re.compile(
    r"(?:"
    # "[xem|coi] [≤3 tokens] [anh|hinh|video|clip|may|em|con]" —
    # "xem ảnh", "coi máy", "xem kỹ máy này", "xem cho rõ ảnh".
    r"\b(?:xem|coi|show)\s+(?:\w+\s+){0,3}"
    r"(?:anh|hinh|video|clip|may|em|con)\b"
    # "[xem|coi] [≤3 tokens] [lai|nua|them|that|khac|chi tiet]" —
    # "xem lại", "xem kỹ lại", "xem nữa", "xem thêm".
    r"|\b(?:xem|coi)\s+(?:\w+\s+){0,3}"
    r"(?:lai|nua|them|that|khac|chi\s*tiet)\b"
    # "[anh|hinh] [≤3 tokens] [lai|that|them|nua|khac|chi tiet]" —
    # "ảnh thật", "ảnh máy nữa", "hình con đó thêm".
    r"|\b(?:anh|hinh)\s+(?:\w+\s+){0,3}"
    r"(?:lai|that|them|chi\s*tiet|nua|khac)\b"
    # "[gui|gửi] [≤3 tokens] [anh|hinh|video|clip]" — "gửi ảnh",
    # "gửi mình ảnh thật", "gửi lại video".
    r"|\b(?:gui|gửi)\s+(?:\w+\s+){0,3}"
    r"(?:anh|hinh|video|clip)\b"
    # English fallbacks
    r"|\bsend\s+(?:me\s+)?(?:more\s+)?(?:photos?|pics?|images?|videos?)\b"
    r"|\b(?:more|another)\s+(?:photos?|pics?|images?)\b"
    r"|\bshow\s+(?:me\s+)?(?:more\s+)?(?:photos?|pics?|images?)\b"
    r")",
    re.I,
)


def _strip_diacritics(text: str) -> str:
    """Lowercase + strip Vietnamese diacritics for fuzzy keyword match."""
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in nfkd if not unicodedata.combining(c))
    # đ/Đ aren't decomposable via NFKD — strip manually so "đẹp" → "dep".
    return plain.lower().replace("đ", "d").replace("Đ", "d")


def _is_photo_request(text: str) -> bool:
    """Best-effort detect: customer asking the shop to send product photos."""
    if not text:
        return False
    return bool(_PHOTO_REQUEST_RE.search(_strip_diacritics(text)))


def _current_focus_product(
    history: list[dict],
    current_text: str,
    default: dict | None,
) -> dict | None:
    """Resolve which product the conversation is *currently* about.

    The post-context that started the DM thread pins one product, but
    customers regularly switch ("post bán 17 PM, khách lại hỏi 15 PM
    nữa") and the "send photos" fallback was sending the *post's*
    product even after the conversation had moved on. We walk back
    through the recent turns instead, picking the latest one that
    references exactly one in_stock catalog item:

      1. The customer's *current* message (lets them say "cho xem ảnh
         15 PM 256GB" and switch instantly).
      2. Recent assistant + user history (newest first). Stops as
         soon as we find a turn that uniquely names one product.
      3. ``default`` (post-context matched product) — used only if
         nothing in the conversation is product-specific.

    A turn that names *several* products (vd bot listing the whole
    stock) is treated as ambiguous and we move further back rather
    than guess; if no turn is unambiguous we fall through to
    ``default``. The caller decides whether to attach a photo at all,
    so worst case is "no photo this turn" rather than the wrong one.
    """
    matches = find_products_in_text(current_text)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # The customer themselves named multiple products this turn —
        # don't second-guess; let the LLM ask which one.
        return None
    for msg in reversed(history[-12:]):
        text = msg.get("content") or ""
        matches = find_products_in_text(text)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Ambiguous turn (vd bot listed several em). Stop and use
            # the post default rather than walking back further into
            # potentially stale single-product mentions.
            return default
    return default

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

    # D6: if the customer is firing several messages within a short
    # window, skip the older ones and only respond to the latest.
    if should_skip_burst(sender):
        log.info("MSG burst-skip from=%s text=%s", sender, text[:80])
        return

    history = load_history(sender)

    ctx_parts: list[str] = []
    post_ctx = load_post_context(sender)
    matched_product: dict | None = None
    if post_ctx:
        block = ["Bài post liên quan (DM này nối tiếp comment khách ở post):"]
        if post_ctx.get("permalink_url"):
            block.append(f"  link: {post_ctx['permalink_url']}")
        if post_ctx.get("post_message"):
            block.append(f"  nội dung: {post_ctx['post_message']}")
        if post_ctx.get("comment_text"):
            block.append(f"  comment gốc của khách: {post_ctx['comment_text']}")

        # C2/D3: resolve the post body to an actual catalog product so
        # the LLM can answer "còn không?" with REAL status (in_stock vs
        # sold) instead of trusting the post text — the post body lags
        # behind the inventory.
        matched_product = match_post_to_product(post_ctx.get("post_message", ""))
        if matched_product:
            block.append(
                "  product_code: " + str(matched_product.get("code", "?"))
            )
            block.append(
                "  STATUS: " + str(matched_product.get("status", "?"))
            )
            price = matched_product.get("price_vnd")
            if price:
                block.append(f"  giá hiện tại kho: {price/1_000_000:.1f}tr")
        ctx_parts.append("\n".join(block))

    # Track which product the *current* DM is about. Often it's the
    # post product, but customers regularly switch ("post bán 17 PM,
    # khách hỏi 15 PM nữa") — surface the latest focus to the LLM so
    # [SEND_PHOTOS:CODE] picks the right code, AND reuse it as the
    # heuristic-fallback target if the LLM forgets the token.
    current_product = _current_focus_product(history, text, matched_product)
    if current_product and (
        not matched_product
        or current_product.get("code") != matched_product.get("code")
    ):
        focus_lines = ["Em khách đang hỏi gần nhất trong DM:"]
        focus_lines.append(
            "  current_product_code: " + str(current_product.get("code", "?"))
        )
        focus_lines.append(
            "  STATUS: " + str(current_product.get("status", "?"))
        )
        price = current_product.get("price_vnd")
        if price:
            focus_lines.append(
                f"  giá hiện tại kho: {price/1_000_000:.1f}tr"
            )
        ctx_parts.append("\n".join(focus_lines))

    ctx_parts.append(inventory_context())
    ctx_parts.append(shop_info_block())

    # Track tool side-effects (photo sends) so the heuristic fallback
    # at the bottom doesn't double-fire after a successful tool call.
    tool_state = {"photos_sent_codes": set()}

    async def _tool_executor(name: str, args: dict) -> str:
        return await _execute_llm_tool(
            name=name,
            args=args,
            sender=sender,
            tool_state=tool_state,
        )

    reply = await draft_reply(
        text,
        context="\n\n".join(ctx_parts),
        history=history,
        tool_executor=_tool_executor,
    )
    if not reply:
        return

    if settings.human_review_queue:
        # TODO: push to Telegram review bot for approval
        log.info("REVIEW draft for %s :: %s", sender, reply)
        return

    # Defense-in-depth: if the LLM mistakenly emits [PRIVATE_REPLY] in a
    # Messenger DM (it shouldn't — that token is only for comment handler),
    # strip the token but keep the full body so the customer still gets the
    # informative DM-style answer. We choose the LONGER of the two bodies
    # since the LLM tends to put detail on the "private" side and a one-line
    # public ack on the "public" side; in a DM we want the detailed one.
    pr_match = _PRIVATE_REPLY_RE.match(reply)
    if pr_match:
        body_a = (pr_match.group(1) or "").strip()
        body_b = (pr_match.group(2) or "").strip()
        reply = (body_a if len(body_a) >= len(body_b) else body_b) or reply

    reply = _scrub_tone(reply)

    photo_code, text_reply = _extract_photo_directive(reply)
    photos_sent = bool(tool_state["photos_sent_codes"])
    if photo_code:
        # If the model already used the ``send_product_photos`` tool
        # this turn, ignore the (legacy) [SEND_PHOTOS:CODE] token to
        # avoid sending the same photos twice. Otherwise validate the
        # code against the live catalog and send via the Send API —
        # the token path is kept as a fallback for any provider that
        # doesn't expose tool-calling.
        if photo_code in tool_state["photos_sent_codes"]:
            log.info(
                "send_photos: token=%s already sent via tool, skipping",
                photo_code,
            )
        elif find_by_code(photo_code):
            await _send_product_photos(sender, photo_code)
            photos_sent = True
            tool_state["photos_sent_codes"].add(photo_code)
        else:
            log.warning(
                "send_photos: rejected hallucinated code=%s for sender=%s",
                photo_code, sender,
            )
            # No photo to send; if there was no fallback text either,
            # send a brief apology so the conversation doesn't stall.
            if not text_reply:
                text_reply = "ể mình check lại ảnh em đó đã nhé bạn"

    # Fallback: the LLM keeps drifting away from emitting the
    # [SEND_PHOTOS:CODE] directive on follow-up requests like "cho xem
    # lại ảnh đi" / "gửi ảnh thật cho mình" / "send more pics". When
    # that happens AND we can pin down a single concrete product the
    # conversation is currently about, attach photos for that product
    # so the bot's promise of "gửi bạn ảnh em đó nè" doesn't read as
    # empty. ``_current_focus_product`` walks back through history to
    # pick the *latest* product the customer/bot was discussing —
    # important when the DM started on post X but the customer has
    # since pivoted to product Y; the old fallback would have sent
    # the post product's photos (the wrong one). When the focus is
    # ambiguous (no recent mention or several at once), we send
    # nothing and rely on the LLM to ask "máy nào".
    if (
        not photos_sent
        and current_product
        and _is_photo_request(text)
    ):
        fallback_code = str(current_product.get("code", "")).upper()
        if (
            fallback_code
            and fallback_code not in tool_state["photos_sent_codes"]
            and find_by_code(fallback_code)
        ):
            log.info(
                "send_photos: heuristic fallback code=%s sender=%s text=%s",
                fallback_code, sender, text[:80],
            )
            await _send_product_photos(sender, fallback_code)
            photos_sent = True
            tool_state["photos_sent_codes"].add(fallback_code)

    if text_reply:
        await fb.send_message(sender, text_reply)
    # remember the *clean* reply (without the [SEND_PHOTOS] token) so the
    # next turn the LLM can refer back to it by code.
    append_turn(sender, text, text_reply or reply)

    # B10/B11: detect order intent (chốt/cọc) or phone number, and
    # forward to the admin Telegram so the human can take over.
    intent = detect_intent(text)
    phone = detect_phone(text)
    if intent in ("checkout", "phone_number", "complaint") or phone:
        try:
            await _notify_admin_intent(
                sender=sender,
                customer_text=text,
                bot_reply=text_reply or reply,
                intent=intent,
                phone=phone,
                matched_product=matched_product,
                photos_sent=photos_sent,
            )
        except Exception:  # noqa: BLE001
            log.exception("notify_admin_intent failed sender=%s", sender)


async def _notify_admin_intent(
    *,
    sender: str,
    customer_text: str,
    bot_reply: str,
    intent: str,
    phone: str | None,
    matched_product: dict | None,
    photos_sent: bool,
) -> None:
    label_map = {
        "checkout": "🔔 KHÁCH CHỐT ĐƠN",
        "phone_number": "📞 KHÁCH ĐỂ LẠI SĐT",
        "complaint": "⚠️ KHIẾU NẠI SAU MUA",
        "general": "ℹ️ KHÁCH NHẮN TIN",
    }
    headline = label_map.get(intent, label_map["general"])
    parts = [
        headline,
        f"sender PSID: {sender}",
    ]
    if phone:
        parts.append(f"SĐT: {phone}")
    if matched_product:
        parts.append(
            "Máy: " + " · ".join(
                str(matched_product.get(k, "")).strip()
                for k in ("code", "model", "storage", "color")
                if matched_product.get(k)
            )
        )
    parts.append("")
    parts.append(f"khách: {customer_text[:600]}")
    parts.append(f"shop trả lời: {bot_reply[:600]}")
    if photos_sent:
        parts.append("(đã gửi ảnh máy cho khách)")
    await notify_admin("\n".join(parts), topic="customer")


_TRAILING_DOT_RE = re.compile(r"([\w\u00C0-\u1EF9])\.(?=\s*\Z|\s*\n)")


def _scrub_tone(text: str) -> str:
    """Apply post-processing tone fixes the LLM keeps drifting away from.

    1. Strip the trailing period at end of message / before newlines —
       the user explicitly asked for chat-style "no period at end".
    2. Strip stray repeated trailing punctuation while we're at it
       (".." → end without dot).
    """
    if not text:
        return text
    # Remove trailing period right before \n or end of string. We do
    # this iteratively because some replies have multiple short
    # sentences each ending in ".".
    cleaned = text
    while True:
        new = _TRAILING_DOT_RE.sub(r"\1", cleaned)
        if new == cleaned:
            break
        cleaned = new
    return cleaned.rstrip()


def _extract_photo_directive(reply: str) -> tuple[str | None, str]:
    """Parse a leading ``[SEND_PHOTOS:CODE]`` token. Returns (code, rest)."""
    m = _SEND_PHOTOS_RE.match(reply)
    if not m:
        return None, reply
    return m.group(1).upper(), _SEND_PHOTOS_RE.sub("", reply, count=1).strip()


async def _execute_llm_tool(
    *,
    name: str,
    args: dict,
    sender: str,
    tool_state: dict,
) -> str:
    """Run a tool requested by the LLM and return a JSON string result.

    Wired into ``llm.draft_reply`` via the ``tool_executor`` callback.
    Each tool returns a small JSON-serialised dict the model can parse
    in the next turn. Errors are returned in-band as ``{"error": ...}``
    rather than raised so a single bad tool call doesn't kill the
    whole reply — the model usually self-corrects on retry.

    Side effects (photo sends) are recorded in ``tool_state`` so the
    later token-based fallback in ``_handle_messenger_event`` can
    avoid re-sending the same photo.
    """
    log.info(
        "tool_call: name=%s sender=%s args=%s",
        name, sender, json.dumps(args, ensure_ascii=False)[:200],
    )

    if name == "lookup_products":
        results = lookup_products(
            model=args.get("model") or None,
            storage=args.get("storage") or None,
            color=args.get("color") or None,
            status=args.get("status") or "in_stock",
        )
        return json.dumps(
            {"matches": results, "count": len(results)},
            ensure_ascii=False,
        )

    if name == "send_product_photos":
        code = (args.get("code") or "").strip().upper()
        if not code:
            return json.dumps({"error": "missing 'code' argument"})
        if code in tool_state["photos_sent_codes"]:
            return json.dumps({
                "status": "already_sent",
                "code": code,
                "note": "ảnh em này đã gửi turn này rồi",
            })
        product = find_by_code(code)
        if not product:
            return json.dumps({
                "error": "code not found in catalog",
                "code": code,
                "hint": "gọi lookup_products trước để lấy code chính xác",
            })
        photos = product_photos(code, max_photos=3)
        if not photos:
            return json.dumps({
                "error": "no photos available for this product",
                "code": code,
            })
        await _send_product_photos(sender, code)
        tool_state["photos_sent_codes"].add(code)
        return json.dumps({
            "status": "sent",
            "code": code,
            "n_photos": len(photos),
        })

    log.warning("tool_call: unknown name=%s", name)
    return json.dumps({"error": f"unknown tool '{name}'"})


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
    deleted: bool = False,
) -> None:
    """Push a moderation alert to the Telegram admin (best-effort)."""
    if deleted:
        flag = "🗑️ ĐÃ XOÁ"
    elif hidden:
        flag = "🚫 ĐÃ ẨN"
    else:
        flag = "⚠️ CẦN XEM"
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
    await notify_admin("\n".join(parts), topic="customer")


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
    delete_labels = _split_labels(settings.comment_auto_delete_labels)
    forward_labels = _split_labels(settings.comment_forward_labels)
    skip_reply_labels = _split_labels(settings.comment_skip_reply_labels)
    min_conf = settings.comment_action_min_confidence

    # Moderate the flagged comment first so it disappears before we
    # even draft a reply. We only act when the classifier is confident
    # — low-confidence calls fall through to normal handling.
    #
    # `delete` wins over `hide` when a label appears in both sets:
    # `is_hidden=true` only hides the comment from public/non-friends;
    # the commenter + friends still see it (FB design). For confirmed
    # spam_toxic the customer wants it *gone* for everyone, so we
    # DELETE instead.
    hidden = False
    deleted = False
    if confidence >= min_conf and label in delete_labels:
        try:
            await fb.delete_comment(comment_id)
            deleted = True
            log.info("COMMENT deleted id=%s label=%s", comment_id, label)
        except Exception:  # noqa: BLE001
            log.exception("delete_comment failed id=%s", comment_id)
    elif confidence >= min_conf and label in hide_labels:
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
            deleted=deleted,
        )

    if not settings.auto_reply_enabled:
        return
    if deleted:
        # Comment is gone — no point drafting a public reply to a
        # comment that no longer exists.
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
    ctx_parts.append(shop_info_block())

    reply = await draft_reply(text, context="\n\n".join(ctx_parts))
    if not reply:
        return
    if settings.human_review_queue:
        log.info("REVIEW comment %s :: %s", comment_id, reply)
        return

    private_msg, public_msg = _extract_private_reply(reply)
    if private_msg:
        private_msg = _scrub_tone(private_msg)
    if public_msg:
        public_msg = _scrub_tone(public_msg)
    if private_msg:
        try:
            resp = await fb.private_reply_to_comment(comment_id, private_msg)
            log.info("COMMENT private_reply id=%s resp=%s", comment_id, resp)
            # FB returns the commenter's PSID in `recipient_id` — use it
            # to seed conversation memory so when the customer replies in
            # Messenger, the LLM still has the post + DM context.
            psid = (resp or {}).get("recipient_id")
            if psid:
                try:
                    post_msg = ""
                    permalink = ""
                    if post_id:
                        post = await fb.fetch_post(post_id)
                        post_msg = (post.get("message") or "").strip()
                        permalink = post.get("permalink_url") or ""
                    set_post_context(
                        str(psid),
                        post_id=post_id,
                        post_message=post_msg,
                        permalink_url=permalink,
                        comment_text=text,
                        private_dm=private_msg,
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "set_post_context failed psid=%s post_id=%s",
                        psid, post_id,
                    )
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
