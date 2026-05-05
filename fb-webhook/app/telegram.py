"""Telegram admin bot integration.

Lets the Page owner drive the FB Page from Telegram chat:

    /post <text>            — publish a text post on the Page feed
    /post_link <url> | <caption>
                            — publish a link with optional caption
    /post_img <caption>     — reply to (or attach in same message) a photo;
                              the bot uploads the photo to the Page feed
    /status                 — webhook + Page Token health
    /whoami                 — show your Telegram chat_id (helps you set
                              TELEGRAM_ADMIN_CHAT_ID once)
    /help, /start           — usage

The bot uses Telegram's Bot API webhook (not long polling). Set the URL
once with `setWebhook` (see scripts/install-telegram-webhook.sh) and Telegram
will POST every update to /webhook/telegram.

Authn:
    1.  `secret_token` echoed back in the `X-Telegram-Bot-Api-Secret-Token`
        header — checked in `app.main` before this module is called.
    2.  Per-update chat_id whitelist (`telegram_admin_chat_id`).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings
from .fb_client import fb

log = logging.getLogger("fb-webhook.telegram")

TG_API = "https://api.telegram.org"

HELP_TEXT = (
    "Bot quản trị Page Apple Shop Siêu Lướt.\n\n"
    "Lệnh:\n"
    "  /post <nội dung>           — đăng bài text\n"
    "  /post_link <url> | <caption> — đăng bài link\n"
    "  /post_img <caption>        — gửi kèm ảnh để đăng bài có ảnh\n"
    "  /status                    — kiểm tra health\n"
    "  /whoami                    — chat_id của bạn\n"
    "  /help                      — hiển thị lại danh sách lệnh"
)


# ---------------------------------------------------------------------------
# Low-level Telegram client
# ---------------------------------------------------------------------------
class TelegramClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=20.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def _base(self) -> str:
        return f"{TG_API}/bot{settings.telegram_bot_token}"

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = None,
        message_thread_id: int | str | None = None,
    ) -> dict[str, Any]:
        if not settings.telegram_bot_token:
            log.warning("telegram_bot_token empty — drop send_message")
            return {}
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if message_thread_id is not None:
            payload["message_thread_id"] = int(message_thread_id)
        r = await self._http.post(f"{self._base}/sendMessage", json=payload)
        if r.status_code >= 400:
            log.error(
                "tg sendMessage failed status=%s body=%s", r.status_code, r.text
            )
        return r.json() if r.text else {}

    async def get_file(self, file_id: str) -> bytes:
        """Resolve a Telegram file_id to a download URL and fetch the bytes."""
        meta = await self._http.get(
            f"{self._base}/getFile", params={"file_id": file_id}
        )
        meta.raise_for_status()
        path = meta.json()["result"]["file_path"]
        url = f"{TG_API}/file/bot{settings.telegram_bot_token}/{path}"
        data = await self._http.get(url, timeout=120.0)
        data.raise_for_status()
        return data.content


tg = TelegramClient()


# ---------------------------------------------------------------------------
# Outbound notifications
# ---------------------------------------------------------------------------
async def notify_admin(text: str, *, topic: str | None = None) -> None:
    """Push an alert to the admin Telegram (best-effort, never raises).

    Routes to the right destination based on optional ``topic`` and the
    settings configured in .env:

    * If ``telegram_admin_group_id`` is set we post into that supergroup,
      using ``telegram_admin_topic_<topic>`` for the message_thread_id
      when present (e.g. topic="customer" → telegram_admin_topic_customer).
    * Otherwise we fall back to ``telegram_admin_chat_id`` (1-1 DM).
    """
    if not settings.telegram_bot_token:
        log.warning("notify_admin: no bot token configured")
        return
    group_id = (settings.telegram_admin_group_id or "").strip()
    chat_id = (settings.telegram_admin_chat_id or "").strip()
    target = group_id or chat_id
    if not target:
        log.warning("notify_admin: no admin chat configured")
        return

    thread_id: str | None = None
    if group_id and topic:
        attr = f"telegram_admin_topic_{topic}"
        thread_id = getattr(settings, attr, "") or None

    try:
        await tg.send_message(target, text, message_thread_id=thread_id)
    except Exception:  # noqa: BLE001
        log.exception("notify_admin failed target=%s topic=%s", target, topic)


# ---------------------------------------------------------------------------
# Update handler
# ---------------------------------------------------------------------------
def _is_admin(chat_id: int | str) -> bool:
    admin = settings.telegram_admin_chat_id
    if not admin:
        return False
    return str(chat_id) == str(admin)


def _post_url(post_id: str) -> str:
    """Build a public URL for a freshly created Page post.

    `post_id` returned by Graph is `<page_id>_<post_id>`; the canonical
    URL is `https://www.facebook.com/<post_id>`.
    """
    return f"https://www.facebook.com/{post_id}"


async def handle_update(update: dict[str, Any]) -> None:
    """Process a single Telegram Update. Never raises — failures are logged."""

    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    photos = msg.get("photo") or []

    if not chat_id:
        return

    log.info(
        "tg update chat=%s text=%s photo=%s",
        chat_id,
        text[:80],
        bool(photos),
    )

    # /whoami works for everyone — that's how the owner finds their id
    # the first time they wire up the bot.
    if text in ("/whoami", "/start"):
        admin_set = bool(settings.telegram_admin_chat_id)
        body = (
            f"chat_id của bạn là: <code>{chat_id}</code>\n\n"
            + ("Bot đã có admin." if admin_set else
               "Chưa có admin. Cho mình biết chat_id này để mình set "
               "TELEGRAM_ADMIN_CHAT_ID trên VPS, sau đó bạn dùng /help.")
        )
        await tg.send_message(chat_id, body, parse_mode="HTML")
        return

    if not _is_admin(chat_id):
        log.warning("tg unauthorized chat=%s text=%s", chat_id, text[:40])
        # Stay silent for non-admins; spammy bots will discover us otherwise.
        return

    if text in ("/help",):
        await tg.send_message(chat_id, HELP_TEXT)
        return

    if text == "/status":
        await _cmd_status(chat_id)
        return

    if text.startswith("/post_link"):
        await _cmd_post_link(chat_id, text[len("/post_link"):].strip())
        return

    if text.startswith("/post_img"):
        await _cmd_post_img(chat_id, text[len("/post_img"):].strip(), photos)
        return

    if text.startswith("/post"):
        await _cmd_post(chat_id, text[len("/post"):].strip(), photos)
        return

    # Plain text message (no leading /command) — be helpful instead of silent
    if text:
        await tg.send_message(
            chat_id,
            "Mình chỉ hiểu các lệnh bắt đầu bằng '/'. Gõ /help để xem danh sách.",
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def _cmd_status(chat_id: int | str) -> None:
    lines = [
        f"auto_reply_enabled = {settings.auto_reply_enabled}",
        f"human_review_queue = {settings.human_review_queue}",
        f"page_id            = {settings.fb_page_id}",
    ]
    try:
        info = await fb.debug_token()
        data = info.get("data", {})
        expires = data.get("expires_at", 0)
        scopes = " ".join(data.get("scopes", []))
        lines.append(
            "page_token         = "
            + ("never expires" if expires == 0 else f"expires_at={expires}")
        )
        lines.append(f"scopes             = {scopes}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"page_token check FAILED: {exc}")
    await tg.send_message(chat_id, "\n".join(lines))


async def _cmd_post(
    chat_id: int | str, body: str, photos: list[dict[str, Any]]
) -> None:
    if photos:
        # Reuse the photo path so /post + photo also works.
        await _cmd_post_img(chat_id, body, photos)
        return
    if not body:
        await tg.send_message(
            chat_id, "Cú pháp: /post <nội dung bài viết>"
        )
        return
    try:
        res = await fb.post_text(body)
    except httpx.HTTPStatusError as exc:
        await tg.send_message(
            chat_id, f"Đăng bài thất bại ({exc.response.status_code}): {exc.response.text[:300]}"
        )
        return
    except Exception as exc:  # noqa: BLE001
        await tg.send_message(chat_id, f"Đăng bài thất bại: {exc}")
        return
    post_id = res.get("id", "?")
    await tg.send_message(
        chat_id,
        f"Đã đăng. id={post_id}\n{_post_url(post_id)}",
    )


async def _cmd_post_link(chat_id: int | str, body: str) -> None:
    if not body:
        await tg.send_message(
            chat_id,
            "Cú pháp: /post_link <url> | <caption (tuỳ chọn)>",
        )
        return
    if "|" in body:
        url_part, caption = (s.strip() for s in body.split("|", 1))
    else:
        url_part, caption = body.strip(), ""
    try:
        res = await fb.post_text(caption or url_part, link=url_part)
    except Exception as exc:  # noqa: BLE001
        await tg.send_message(chat_id, f"Đăng link thất bại: {exc}")
        return
    post_id = res.get("id", "?")
    await tg.send_message(chat_id, f"Đã đăng link. {_post_url(post_id)}")


async def _cmd_post_img(
    chat_id: int | str, caption: str, photos: list[dict[str, Any]]
) -> None:
    if not photos:
        await tg.send_message(
            chat_id,
            "Gửi 1 ảnh (kèm caption ở phần ghi chú ảnh: '/post_img caption ở đây') "
            "hoặc reply ảnh đã có và gõ /post_img <caption>.",
        )
        return
    # Telegram sends multiple sizes — pick the largest.
    largest = max(photos, key=lambda p: p.get("file_size") or 0)
    file_id = largest["file_id"]
    try:
        data = await tg.get_file(file_id)
    except Exception as exc:  # noqa: BLE001
        await tg.send_message(chat_id, f"Tải ảnh từ Telegram thất bại: {exc}")
        return
    try:
        res = await fb.post_photo_bytes(data, caption=caption)
    except httpx.HTTPStatusError as exc:
        await tg.send_message(
            chat_id, f"Đăng ảnh thất bại ({exc.response.status_code}): {exc.response.text[:300]}"
        )
        return
    except Exception as exc:  # noqa: BLE001
        await tg.send_message(chat_id, f"Đăng ảnh thất bại: {exc}")
        return
    post_id = res.get("post_id") or res.get("id", "?")
    await tg.send_message(
        chat_id, f"Đã đăng ảnh. id={post_id}\n{_post_url(post_id)}"
    )
