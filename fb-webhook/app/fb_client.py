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


fb = FacebookClient()
