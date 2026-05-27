import logging
import os

import httpx

logger = logging.getLogger(__name__)


class WhatsAppService:
    def __init__(self, api_url: str, api_key: str, backend_url: str):
        self.api_url = api_url
        self.api_key = api_key
        self.backend_url = backend_url.rstrip("/")

    async def send_image(self, to: str, filename: str, caption: str = "", quoted_msg_id: str | None = None) -> dict:
        media_url = f"{self.backend_url}/images/{filename}"
        payload: dict = {
            "to": to,
            "mediaUrl": media_url,
            "mediaType": "image",
            "caption": caption,
            "fileName": filename,
        }
        if quoted_msg_id:
            payload["quotedMessageId"] = quoted_msg_id
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.api_url,
                json=payload,
                headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            logger.info(f"Sent image {filename} to {to} — status {resp.status_code}")
            return resp.json()

    async def send_top_matches(self, to: str, matches: list[dict], scores: list[float]):
        for i, (match, score) in enumerate(zip(matches, scores)):
            filename = os.path.basename(match["local_path"])
            caption = f"Match {i + 1}/5 — {match['name']} ({score * 100:.1f}% similar)"
            try:
                await self.send_image(to, filename, caption)
            except Exception as e:
                logger.error(f"Failed to send match {i + 1}: {e}")
