import asyncio
import hashlib
import hmac
import json
import logging
import os

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

import state
from config import settings

router = APIRouter(prefix="/guru-kripa")
logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {"image", "document", "text"}

# ── Deduplication — ignore repeated webhook deliveries ───────────────────────
from collections import deque
_seen_ids: deque = deque(maxlen=500)  # keeps last 500 message IDs in memory

# ── Queue — one request processed at a time to protect the number ────────────
_queue: asyncio.Queue = asyncio.Queue()
_worker_started = False

# Delay between sending each WhatsApp message (seconds)
SEND_DELAY = 2.0


async def _worker():
    """Consumes the queue sequentially — never processes two requests in parallel."""
    while True:
        image_url, text, msg_type, sender = await _queue.get()
        try:
            await _process(image_url, text, msg_type, sender)
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
        finally:
            _queue.task_done()


@router.on_event("startup")
async def start_worker():
    global _worker_started
    if not _worker_started:
        asyncio.create_task(_worker())
        _worker_started = True
        logger.info("Guru Kripa queue worker started")


@router.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()

    if settings.guru_kripa_webhook_secret:
        sig = request.headers.get("x-waumfy-signature", "").removeprefix("sha256=")
        expected = hmac.new(
            settings.guru_kripa_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning(f"Guru Kripa signature mismatch — got: {sig}")
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

    # Enqueue — webhook returns immediately, processing happens sequentially
    await _queue.put((image_url, text, msg_type, sender))
    return {"status": "queued", "position": _queue.qsize()}


async def _process(image_url: str | None, text: str | None, msg_type: str, sender: str):
    loop = asyncio.get_event_loop()

    # Download media
    image_bytes = None
    if image_url:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    image_url,
                    headers={"X-API-Key": settings.wa_api_key},
                )
                resp.raise_for_status()
                image_bytes = resp.content
        except Exception as e:
            logger.error(f"Media download failed: {e}")

    if not image_bytes and not text:
        logger.warning("Nothing to process after download")
        return

    if not state.openai_svc:
        logger.error("OpenAI service not initialised — add OPENAI_API_KEY to .env")
        return

    # GPT-4o analyses query — returns (type, value)
    query_type, query_value = await state.openai_svc.analyze_query(image_bytes, text, msg_type)
    logger.info(f"Query analysis → type={query_type} value={query_value}")

    # ── Stock number search ───────────────────────────────────────────────────
    if query_type == "stock":
        stock_number = query_value
        item = state.ims.find_by_stock_number(stock_number)

        if not item:
            await _reply_text(sender, f"❌ Item *{stock_number}* was not found in our inventory.")
            return

        available, remark = state.ims.check_availability(stock_number)

        if remark:
            await _reply_text(sender, f"❌ Item *{stock_number}* is already reserved.\n_{remark}_")
            return

        if not available:
            await _reply_text(sender, f"❌ Item *{stock_number}* is currently not available.")
            return

        filename = os.path.basename(item["local_path"])
        caption = f"✅ *{item['name'].rsplit('.', 1)[0]}* — exact match"
        await asyncio.sleep(SEND_DELAY)
        await state.wa.send_image(sender, filename, caption)
        logger.info(f"Sent exact match {stock_number} to {sender}")

    # ── Color search ─────────────────────────────────────────────────────────
    elif query_type == "color":
        color = query_value
        matches = state.ims.find_by_color(color)

        if not matches:
            await _reply_text(sender, f"❌ No *{color}* items found in our inventory.")
            return

        for i, item in enumerate(matches):
            available, remark = state.ims.check_availability(
                item["name"].rsplit(".", 1)[0]
            )
            if not available or remark:
                continue  # skip unavailable items silently
            filename = os.path.basename(item["local_path"])
            caption = f"{item['name'].rsplit('.', 1)[0]} ({color})"
            await asyncio.sleep(SEND_DELAY)
            try:
                await state.wa.send_image(sender, filename, caption)
            except Exception as e:
                logger.error(f"Failed to send color match {i+1}: {e}")

    # ── Not found ────────────────────────────────────────────────────────────
    else:
        await _reply_text(
            sender,
            "❌ Could not identify the item. Please send a clearer image with the item "
            "code visible, or mention the stock number or color in your message.",
        )


async def _reply_text(to: str, message: str):
    url = settings.wa_text_api_url or settings.wa_api_url
    payload = {"to": to, "text": message}
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


@router.post("/ims/{stock_number}/reserve")
async def reserve_item(stock_number: str, client_name: str = ""):
    state.ims.set_availability(
        stock_number,
        available=True,
        client_remark=f"Reserved — {client_name}" if client_name else "Reserved",
    )
    return {"status": "reserved", "stock_number": stock_number.upper()}


@router.post("/ims/{stock_number}/release")
async def release_item(stock_number: str):
    state.ims.set_availability(stock_number, available=True, client_remark=None)
    return {"status": "released", "stock_number": stock_number.upper()}


@router.post("/ims/{stock_number}/sold")
async def mark_sold(stock_number: str):
    state.ims.set_availability(stock_number, available=False, client_remark="Sold")
    return {"status": "sold", "stock_number": stock_number.upper()}
