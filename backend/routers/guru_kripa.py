import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections import deque

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

import state
from config import settings
from core.security import media_host_allowed, require_admin
from services.openai_service import profile_to_embed_text

router = APIRouter(prefix="/guru-kripa")
logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {"image", "document", "text"}

# ── Deduplication — ignore repeated webhook deliveries ───────────────────────
_seen_ids: deque = deque(maxlen=500)  # keeps last 500 message IDs in memory

# ── Queue — one request processed at a time to protect the number ────────────
_queue: asyncio.Queue = asyncio.Queue()
_worker_started = False
_processing_task: asyncio.Task | None = None  # currently running _process task

# Delay between sending each WhatsApp message (seconds)
SEND_DELAY = 5.0

# Cosine score at/above which the top semantic result is called a "strong match".
STRONG_MATCH = 0.55


async def _worker():
    """Consumes the queue sequentially — never processes two requests in parallel."""
    global _processing_task
    while True:
        args = await _queue.get()
        try:
            _processing_task = asyncio.ensure_future(_process(*args))
            await _processing_task
        except asyncio.CancelledError:
            logger.info("Processing cancelled by STOP command")
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
        finally:
            _processing_task = None
            _queue.task_done()


async def ensure_worker_started():
    """Called from the app lifespan (main.py) — starts the single queue worker once."""
    global _worker_started
    if not _worker_started:
        asyncio.create_task(_worker())
        _worker_started = True
        logger.info("Guru Kripa queue worker started")


@router.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()

    # Fail closed: refuse if no secret is configured (SEC-01).
    if not settings.guru_kripa_webhook_secret:
        logger.error("guru_kripa_webhook_secret not set — rejecting webhook")
        raise HTTPException(status_code=503, detail="webhook not configured")
    sig = request.headers.get("x-waumfy-signature", "").removeprefix("sha256=")
    expected = hmac.new(
        settings.guru_kripa_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        logger.warning("Guru Kripa signature mismatch")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    data = payload.get("data", {})
    msg_type = data.get("type", "unknown")
    sender = data.get("from", "")

    logger.info(f"Guru Kripa webhook — type={msg_type} from={sender} queue_size={_queue.qsize()}")

    if msg_type not in SUPPORTED_TYPES:
        return {"status": "skipped", "reason": f"unsupported type: {msg_type}"}

    if settings.guru_kripa_group_id and sender != settings.guru_kripa_group_id:
        return {"status": "skipped", "reason": "wrong group"}

    # Deduplicate — aumpfy sometimes fires the same webhook twice
    msg_id = data.get("messageId") or data.get("id")
    if msg_id:
        if msg_id in _seen_ids:
            logger.info(f"Duplicate message {msg_id} — skipped")
            return {"status": "skipped", "reason": "duplicate"}
        _seen_ids.append(msg_id)

    image_url = data.get("mediaUrl") if msg_type in ("image", "document") else None
    text = data.get("body") or None

    if not image_url and not text:
        return {"status": "skipped", "reason": "no content"}

    # ── STOP command ─────────────────────────────────────────────────────────
    if msg_type == "text" and text and text.strip().upper() == "STOP":
        stopped = False
        if _processing_task and not _processing_task.done():
            _processing_task.cancel()
            stopped = True
        remaining = _queue.qsize()
        if stopped and remaining > 0:
            reply = f"⛔ Search stopped. {remaining} request(s) still in queue and will be processed."
        elif stopped:
            reply = "⛔ Search stopped."
        elif remaining > 0:
            reply = f"ℹ️ Nothing was processing. {remaining} request(s) in queue."
        else:
            reply = "ℹ️ Nothing to stop — no active search or pending requests."
        background.add_task(_reply_text, sender, reply)
        return {"status": "stopped"}

    await _queue.put((image_url, text, msg_type, sender, msg_id))
    return {"status": "queued", "position": _queue.qsize()}


async def _process(image_url: str | None, text: str | None, msg_type: str, sender: str, msg_id: str | None = None):
    # Download media (SSRF guard: only from trusted media hosts)
    image_bytes = None
    if image_url and media_host_allowed(image_url):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(image_url, headers={"X-API-Key": settings.wa_api_key})
                resp.raise_for_status()
                image_bytes = resp.content
        except Exception as e:
            logger.error(f"Media download failed: {e}")

    if not image_bytes and not text:
        logger.warning("Nothing to process after download")
        return

    if not state.openai_svc:
        logger.error("OpenAI service not initialised")
        return

    # ── IMAGE query (optionally with text) ───────────────────────────────────
    if image_bytes:
        profile = await state.openai_svc.analyze_image_profile(image_bytes, msg_type)

        # 1. Stock code visible on a label → exact lookup
        stock = (profile.get("stock_number") or "").strip() if profile else ""
        if stock:
            await _send_exact(sender, stock, msg_id)
            return

        # 2. Visual/semantic match — embed the profile (+ any accompanying text)
        if profile:
            embed_text = profile_to_embed_text(profile)
            if text:
                embed_text = f"{embed_text} {text}".strip()  # "same item in black" etc.
            query_vec = await state.openai_svc.embed_text(embed_text)
            results = state.cache.find_semantic(query_vec, k=5, min_score=settings.min_match_score)
            if results:
                await _send_matches(sender, results, msg_id)
                return

        # 3. Fallback — keyword search from text or profile category/colors
        fallback_q = text or " ".join((profile or {}).get("colors", []) + [(profile or {}).get("category", "")])
        matches = state.ims.find_by_color(fallback_q, limit=5)
        if matches:
            await _send_matches(sender, [(m, None) for m in matches], msg_id)
            return

        await _reply_text(sender, "❌ Could not identify the item. Please send a clearer image or type the stock number.", msg_id)
        return

    # ── TEXT query ───────────────────────────────────────────────────────────
    query_type, query_value = await state.openai_svc.analyze_query(None, text, "text")
    logger.info(f"Text query analysis -> type={query_type} value={query_value}")

    if query_type == "stock":
        await _send_exact(sender, query_value, msg_id)
        return

    # color / describe → semantic search on the unified vector space
    raw_query = text or query_value or ""
    results = await _semantic_search(raw_query, query_value or raw_query)
    if not results:
        await _reply_text(sender, f"❌ No matching items found for *{query_value or raw_query}*.", msg_id)
        return
    await _send_matches(sender, results, msg_id)


async def _semantic_search(raw_query: str, keyword_fallback: str) -> list[tuple[dict, float | None]]:
    """Embedding search first, then keyword fallback. Returns (item, score) pairs."""
    query_vec = await state.openai_svc.embed_text(raw_query)
    results = state.cache.find_semantic(query_vec, k=5, min_score=settings.min_match_score)
    if results:
        logger.info(f"Semantic search for '{raw_query}' returned {len(results)} results")
        return results
    logger.info(f"Falling back to keyword search for '{keyword_fallback}'")
    return [(m, None) for m in state.ims.find_by_color(keyword_fallback, limit=5)]


async def _send_exact(sender: str, stock: str, msg_id: str | None):
    item = state.ims.find_by_stock_number(stock)
    if not item:
        await _reply_text(sender, f"❌ Item *{stock}* was not found in our inventory.", msg_id)
        return
    available, remark = state.ims.check_availability(stock)
    if remark:
        await _reply_text(sender, f"❌ Item *{stock}* is already reserved.\n_{remark}_", msg_id)
        return
    if not available:
        await _reply_text(sender, f"❌ Item *{stock}* is currently not available.", msg_id)
        return
    filename = os.path.basename(item["local_path"])
    await asyncio.sleep(SEND_DELAY)
    await state.wa.send_image(sender, filename, f"✅ *{stock}* — exact match", quoted_msg_id=msg_id)


async def _send_matches(sender: str, results: list[tuple[dict, float | None]], msg_id: str | None):
    """Send up to 5 available matches with confidence captions. results = (item, score|None)."""
    sent = 0
    for item, score in results:
        stock = item["name"].rsplit(".", 1)[0]
        available, remark = state.ims.check_availability(stock)
        if not available or remark:
            continue
        filename = os.path.basename(item["local_path"])
        tags = state.ims.tags_for(item)
        tag_str = ", ".join(tags[:4])

        if score is None:
            caption = stock + (f"\n{tag_str}" if tag_str else "")
        else:
            pct = round(score * 100, 1)
            label = "✅ best match" if (sent == 0 and score >= STRONG_MATCH) else f"{pct}% similar"
            caption = f"*{stock}* — {label}" + (f"\n{tag_str}" if tag_str else "")

        await asyncio.sleep(SEND_DELAY)
        try:
            await state.wa.send_image(sender, filename, caption, quoted_msg_id=msg_id if sent == 0 else None)
            sent += 1
        except Exception as e:
            logger.error(f"Failed to send match: {e}")

    if sent == 0:
        await _reply_text(sender, "❌ No available matches found.", msg_id)
    else:
        logger.info(f"Sent {sent} matches to {sender}")


async def _reply_text(to: str, message: str, quoted_msg_id: str | None = None):
    url = settings.wa_text_api_url or settings.wa_api_url
    payload: dict = {"to": to, "text": message}
    if quoted_msg_id:
        payload["quotedMessageId"] = quoted_msg_id
    try:
        await asyncio.sleep(SEND_DELAY)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"X-API-Key": settings.wa_api_key, "Content-Type": "application/json"},
            )
            logger.info(f"Text reply to {to} — status {resp.status_code} — body {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send text reply: {e}")


# ── Admin endpoints ──────────────────────────────────────────────────────────

@router.get("/ims")
async def get_ims():
    return state.ims.all_remarks()


@router.post("/ims/{stock_number}/reserve", dependencies=[Depends(require_admin)])
async def reserve_item(stock_number: str, client_name: str = ""):
    state.ims.set_availability(
        stock_number,
        available=True,
        client_remark=f"Reserved — {client_name}" if client_name else "Reserved",
    )
    return {"status": "reserved", "stock_number": stock_number.upper()}


@router.post("/ims/{stock_number}/release", dependencies=[Depends(require_admin)])
async def release_item(stock_number: str):
    state.ims.set_availability(stock_number, available=True, client_remark=None)
    return {"status": "released", "stock_number": stock_number.upper()}


@router.post("/ims/{stock_number}/sold", dependencies=[Depends(require_admin)])
async def mark_sold(stock_number: str):
    state.ims.set_availability(stock_number, available=False, client_remark="Sold")
    return {"status": "sold", "stock_number": stock_number.upper()}
