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
_processing_task: asyncio.Task | None = None  # currently running _process task

# Delay between sending each WhatsApp message (seconds)
SEND_DELAY = 5.0


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

    # Enqueue — webhook returns immediately, processing happens sequentially
    await _queue.put((image_url, text, msg_type, sender, msg_id))
    return {"status": "queued", "position": _queue.qsize()}


async def _process(image_url: str | None, text: str | None, msg_type: str, sender: str, msg_id: str | None = None):
    loop = asyncio.get_event_loop()

    # Download media
    image_bytes = None
    if image_url:
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

    # ── IMAGE query: CNN similarity first ────────────────────────────────────
    if image_bytes:
        # 1. Try to read stock number from label via GPT
        query_type, query_value = await state.openai_svc.analyze_query(image_bytes, text, msg_type)
        logger.info(f"Query analysis -> type={query_type} value={query_value}")

        if query_type == "stock":
            # GPT read the label clearly — do exact stock lookup
            item = state.ims.find_by_stock_number(query_value)
            if not item:
                await _reply_text(sender, f"❌ Item *{query_value}* was not found in our inventory.", msg_id)
                return
            available, remark = state.ims.check_availability(query_value)
            if remark:
                await _reply_text(sender, f"❌ Item *{query_value}* is already reserved.\n_{remark}_", msg_id)
                return
            if not available:
                await _reply_text(sender, f"❌ Item *{query_value}* is currently not available.", msg_id)
                return
            filename = os.path.basename(item["local_path"])
            await asyncio.sleep(SEND_DELAY)
            await state.wa.send_image(sender, filename, f"✅ *{query_value}* — exact match", quoted_msg_id=msg_id)
            return

        # 2. No stock number read — use CNN to find exact + similar matches
        emb_matrix = await loop.run_in_executor(None, state.cache.get_embedding_matrix)
        if emb_matrix.size > 0:
            try:
                query_emb = await loop.run_in_executor(None, state.cnn.extract, image_bytes)
                top_k = await loop.run_in_executor(None, state.cnn.find_top_k, query_emb, emb_matrix, 5)
            except Exception as e:
                logger.error(f"CNN extraction failed: {e}")
                top_k = []

            sent = 0
            for idx, score in top_k:
                item = state.cache.images[idx]
                stock = item["name"].rsplit(".", 1)[0]
                available, remark = state.ims.check_availability(stock)
                if not available or remark:
                    continue
                filename = os.path.basename(item["local_path"])
                pct = round(score * 100, 1)
                tags = state.ims._color_index.get(item["name"], [])
                tag_str = ", ".join(tags[:4]) if tags else ""

                if score >= 0.90 and sent == 0:
                    caption = f"✅ *{stock}* — exact match ({pct}%)" + (f"\n{tag_str}" if tag_str else "")
                else:
                    caption = f"*{stock}* — {pct}% similar" + (f"\n{tag_str}" if tag_str else "")

                await asyncio.sleep(SEND_DELAY)
                try:
                    await state.wa.send_image(sender, filename, caption, quoted_msg_id=msg_id if sent == 0 else None)
                    sent += 1
                except Exception as e:
                    logger.error(f"Failed to send CNN match: {e}")

            if sent > 0:
                logger.info(f"Sent {sent} CNN matches to {sender}")
                return

        # 3. CNN cache empty or no results — fall back to semantic search
        if query_type == "color":
            matches = await _semantic_search(query_value, query_value)
            if not matches:
                await _reply_text(sender, f"❌ No matching items found.", msg_id)
                return
            for i, item in enumerate(matches):
                available, remark = state.ims.check_availability(item["name"].rsplit(".", 1)[0])
                if not available or remark:
                    continue
                filename = os.path.basename(item["local_path"])
                tags = state.ims._color_index.get(item["name"], [])
                tag_str = ", ".join(tags[:4]) if tags else query_value
                caption = f"{item['name'].rsplit('.', 1)[0]} ({tag_str})"
                await asyncio.sleep(SEND_DELAY)
                try:
                    await state.wa.send_image(sender, filename, caption, quoted_msg_id=msg_id if i == 0 else None)
                except Exception as e:
                    logger.error(f"Failed to send match {i+1}: {e}")
            return

        await _reply_text(sender, "❌ Could not identify the item. Please send a clearer image or type the stock number.", msg_id)
        return

    # ── TEXT query: GPT analysis ──────────────────────────────────────────────
    query_type, query_value = await state.openai_svc.analyze_query(None, text, "text")
    logger.info(f"Text query analysis -> type={query_type} value={query_value}")

    if query_type == "stock":
        item = state.ims.find_by_stock_number(query_value)
        if not item:
            await _reply_text(sender, f"❌ Item *{query_value}* was not found in our inventory.", msg_id)
            return
        available, remark = state.ims.check_availability(query_value)
        if remark:
            await _reply_text(sender, f"❌ Item *{query_value}* is already reserved.\n_{remark}_", msg_id)
            return
        if not available:
            await _reply_text(sender, f"❌ Item *{query_value}* is currently not available.", msg_id)
            return
        filename = os.path.basename(item["local_path"])
        await asyncio.sleep(SEND_DELAY)
        await state.wa.send_image(sender, filename, f"✅ *{query_value}* — exact match", quoted_msg_id=msg_id)

    elif query_type == "color":
        matches = await _semantic_search(text or query_value, query_value)
        if not matches:
            await _reply_text(sender, f"❌ No matching items found for *{query_value}*.", msg_id)
            return
        for i, item in enumerate(matches):
            available, remark = state.ims.check_availability(item["name"].rsplit(".", 1)[0])
            if not available or remark:
                continue
            filename = os.path.basename(item["local_path"])
            tags = state.ims._color_index.get(item["name"], [])
            tag_str = ", ".join(tags[:4]) if tags else query_value
            caption = f"{item['name'].rsplit('.', 1)[0]} ({tag_str})"
            await asyncio.sleep(SEND_DELAY)
            try:
                await state.wa.send_image(sender, filename, caption, quoted_msg_id=msg_id if i == 0 else None)
            except Exception as e:
                logger.error(f"Failed to send match {i+1}: {e}")

    else:
        await _reply_text(sender, "❌ Could not identify the item. Please mention the stock number or describe the item.", msg_id)


async def _semantic_search(raw_query: str, keyword_fallback: str) -> list[dict]:
    """Try semantic embedding search first, fall back to keyword search."""
    if state.openai_svc and state.ims._text_embeddings:
        embedding = await state.openai_svc.embed_text(raw_query)
        if embedding:
            results = state.ims.find_by_semantic(embedding, limit=5)
            if results:
                logger.info(f"Semantic search for '{raw_query}' returned {len(results)} results")
                return results
    # Fallback to keyword search
    logger.info(f"Falling back to keyword search for '{keyword_fallback}'")
    return state.ims.find_by_color(keyword_fallback, limit=5)


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
