import hashlib
import hmac
import json
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

import state
from config import settings
from core.security import media_host_allowed
from services.openai_service import profile_to_embed_text

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

    # Verify the request is genuinely from aumpfy. Fail closed (SEC-01).
    if not settings.webhook_secret:
        logger.error("webhook_secret not set — rejecting webhook")
        raise HTTPException(status_code=503, detail="webhook not configured")
    sig = request.headers.get("x-waumfy-signature", "").removeprefix("sha256=")
    expected = hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        logger.warning("Signature mismatch")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)

    image_url = _extract_image_url(payload)
    sender = _extract_sender(payload)
    # Log only non-sensitive metadata, never the full payload (SEC-04)
    logger.info(f"Webhook — sender={sender} has_image={bool(image_url)}")

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
    # Download the incoming image (SSRF guard: only from trusted media hosts)
    if not media_host_allowed(image_url):
        return
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

    if not state.openai_svc:
        logger.error("OpenAI service not initialised")
        return

    # Vision profile → embed → semantic search (replaces the old CNN vector path)
    profile = await state.openai_svc.analyze_image_profile(image_bytes)
    if not profile:
        logger.warning("Could not profile incoming image")
        return

    query_vec = await state.openai_svc.embed_text(profile_to_embed_text(profile))
    results = state.cache.find_semantic(query_vec, k=5, min_score=settings.min_match_score)
    if not results:
        logger.warning("No matches — cache may be empty, run POST /admin/refresh first")
        return

    matches = [item for item, _ in results]
    scores = [score for _, score in results]

    # Send back to the same group
    await state.wa.send_top_matches(sender, matches, scores)

    # Log for dashboard
    state.cache.log_match(sender, image_url, matches, scores)
    logger.info(f"Sent {len(matches)} matches to {sender}")
