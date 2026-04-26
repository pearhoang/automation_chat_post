"""Thin wrapper around the Facebook Graph API for posting replies."""

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


class FacebookClient:
    def __init__(self) -> None:
        self.base = f"https://graph.facebook.com/{settings.fb_graph_version}"
        self.page_token = settings.fb_page_token
        self.page_id = settings.fb_page_id
        self._http = httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_message(self, recipient_id: str, text: str) -> dict:
        """Send a Messenger message inside the 24h response window."""
        url = f"{self.base}/me/messages"
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
            "messaging_type": "RESPONSE",
        }
        params = {"access_token": self.page_token}
        r = await self._http.post(url, params=params, json=payload)
        if r.status_code >= 400:
            log.error("send_message failed status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()

    async def reply_comment(self, comment_id: str, text: str) -> dict:
        """Public reply on a Page post comment."""
        url = f"{self.base}/{comment_id}/comments"
        params = {"access_token": self.page_token}
        r = await self._http.post(url, params=params, data={"message": text})
        if r.status_code >= 400:
            log.error("reply_comment failed status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()

    async def hide_comment(self, comment_id: str) -> dict:
        url = f"{self.base}/{comment_id}"
        params = {"access_token": self.page_token}
        r = await self._http.post(url, params=params, data={"is_hidden": "true"})
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Posting on the Page feed
    # ------------------------------------------------------------------
    async def post_text(self, message: str, link: str | None = None) -> dict:
        """Publish a text (or text+link) post on the Page feed.

        Requires the `pages_manage_posts` scope on the Page Token.
        """
        url = f"{self.base}/{self.page_id}/feed"
        data: dict[str, str] = {"message": message}
        if link:
            data["link"] = link
        params = {"access_token": self.page_token}
        r = await self._http.post(url, params=params, data=data)
        if r.status_code >= 400:
            log.error("post_text failed status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()

    async def post_photo_url(self, photo_url: str, caption: str = "") -> dict:
        """Publish a photo to the Page feed by URL (Graph downloads the image).

        Requires `pages_manage_posts`. Returns `{post_id, id}`.
        """
        url = f"{self.base}/{self.page_id}/photos"
        data: dict[str, str] = {"url": photo_url}
        if caption:
            data["caption"] = caption
        params = {"access_token": self.page_token}
        r = await self._http.post(url, params=params, data=data, timeout=60.0)
        if r.status_code >= 400:
            log.error("post_photo_url failed status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()

    async def post_photo_bytes(
        self, photo_bytes: bytes, filename: str = "image.jpg", caption: str = ""
    ) -> dict:
        """Publish a photo by uploading raw bytes (multipart)."""
        url = f"{self.base}/{self.page_id}/photos"
        files = {"source": (filename, photo_bytes, "application/octet-stream")}
        data: dict[str, str] = {}
        if caption:
            data["caption"] = caption
        params = {"access_token": self.page_token}
        r = await self._http.post(
            url, params=params, data=data, files=files, timeout=120.0
        )
        if r.status_code >= 400:
            log.error("post_photo_bytes failed status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()

    async def debug_token(self) -> dict:
        """Inspect the Page Token (expiry, scopes). Used by /status."""
        url = f"{self.base}/debug_token"
        params = {
            "input_token": self.page_token,
            "access_token": self.page_token,
        }
        r = await self._http.get(url, params=params)
        r.raise_for_status()
        return r.json()


fb = FacebookClient()
