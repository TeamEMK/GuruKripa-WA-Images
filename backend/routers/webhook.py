import asyncio
import hashlib
import hmac
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

import state
from config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _extract_image_url(payload: dict) -> str | None:
    data = payload.get("data", {})
    # aumpfy format: data.type == "image", data.mediaUrl
    if data.get("type") == "image" and data.get("mediaUrl"):
        return data["mediaUrl"]
    # flat format
    if payload.get("type") == "image" and payload.get("mediaUrl"):
        return payload["mediaUrl"]
    # Baileys/WASocket nested format
    msg = data.get("message", {})
    img = msg.get("imageMessage", {})
    return img.get("url") or img.get("downloadUrl") or None


def _extract_sender(payload: dict) -> str | None:
    data = payload.get("data", {})
    return (
        payload.get("from")
        or data.get("from")
        or data.get("key", {}).get("remoteJid")
        or None
    )


def _is_group(jid: str) -> bool:
    return jid is not None and "@g.us" in jid


@router.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()

    # Verify the request is genuinely from aumpfy
    if settings.webhook_secret:
        sig = request.headers.get("x-waumfy-signature", "")
        sig = sig.removeprefix("sha256=")  # handle both plain hex and "sha256=..." formats
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning(f"Signature mismatch — got: {sig}")
            raise HTTPException(status_code=401, detail="Invalid signature")

    import json
    payload = json.loads(body)
    logger.info(f"Webhook payload: {payload}")

    image_url = _extract_image_url(payload)
    sender = _extract_sender(payload)

    if not image_url:
        return {"status": "skipped", "reason": "no image found in payload"}
    if not _is_group(sender):
        return {"status": "skipped", "reason": "not a group message"}
    if settings.wa_images_group_id and sender != settings.wa_images_group_id:
        return {"status": "skipped", "reason": "wrong group"}
    # If this group is also the Guru Kripa group, let Guru Kripa handle it (avoid double response)
    if settings.guru_kripa_group_id and sender == settings.guru_kripa_group_id:
        return {"status": "skipped", "reason": "handled by guru kripa"}

    background.add_task(_process, image_url, sender)
    return {"status": "processing", "sender": sender}


async def _process(image_url: str, sender: str):
    loop = asyncio.get_event_loop()

    # Download the incoming image (aumpfy media URLs need the API key)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                image_url,
                headers={"X-API-Key": settings.wa_api_key},
            )
            resp.raise_for_status()
            image_bytes = resp.content
    except Exception as e:
        logger.error(f"Failed to download image: {e}")
        return

    # CNN extract (CPU-bound → thread pool)
    try:
        query_emb = await loop.run_in_executor(None, state.cnn.extract, image_bytes)
    except Exception as e:
        logger.error(f"Feature extraction failed: {e}")
        return

    # Get embedding matrix
    emb_matrix = await loop.run_in_executor(None, state.cache.get_embedding_matrix)
    if emb_matrix.size == 0:
        logger.warning("Cache is empty — run POST /admin/refresh first")
        return

    # Find top 5 similar
    top_k = await loop.run_in_executor(None, state.cnn.find_top_k, query_emb, emb_matrix, 5)
    if not top_k:
        return

    indices = [i for i, _ in top_k]
    scores = [s for _, s in top_k]
    matches = state.cache.get_by_indices(indices)

    # Send back to the same group
    await state.wa.send_top_matches(sender, matches, scores)

    # Log for dashboard
    state.cache.log_match(sender, image_url, matches, scores)
    logger.info(f"Sent {len(matches)} matches to {sender}")
