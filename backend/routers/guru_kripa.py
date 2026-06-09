import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import deque

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

import state
from config import settings
from core.security import media_host_allowed, require_admin
from services.openai_service import profile_caption, profile_to_embed_text

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

# ── Per-sender image context — enables follow-up text queries ─────────────────
# When a customer sends an image, we store the profile + embed text for 30 min.
# A subsequent text like "Need matching jhumki tops" blends the image's style
# with the requested type to find complementary pieces.
_CONTEXT_TTL = 1800  # 30 minutes
_sender_context: dict[str, dict] = {}


def _set_sender_context(sender: str, embed_text: str, vec: list[float]) -> None:
    _sender_context[sender] = {"embed_text": embed_text, "vec": vec, "ts": time.time()}


def _get_sender_context(sender: str) -> dict | None:
    ctx = _sender_context.get(sender)
    if ctx and time.time() - ctx["ts"] < _CONTEXT_TTL:
        return ctx
    if ctx:
        del _sender_context[sender]
    return None

# Cosine score at/above which the top semantic result is called a "strong match".
STRONG_MATCH = 0.55

# Fulfillment policy for an image query:
#   exact match on label  → send the exact piece + up to MAX_SIMILAR_AFTER_EXACT similar
#   no exact match        → send up to MAX_MATCHES best matches
MAX_MATCHES = 5
MAX_SIMILAR_AFTER_EXACT = 4
# A result only counts as "matching" if its cosine is within RELEVANCE_GAP of the
# best hit. Weaker tail results are dropped, so when only 2-3 pieces genuinely
# match we send 2-3 — never padded up to 5 with loosely-related items.
RELEVANCE_GAP = 0.10


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

    # ── IMAGE query (optionally with a caption) ──────────────────────────────
    #
    # Three modes:
    #  • Image + caption text → understand the image's STYLE, then return items
    #    of the TYPE requested in the caption (category/weight/length filters
    #    are derived from the text; the embedding is from the image alone).
    #  • Image only            → find up to 5 items that match the image's style.
    #  • Text only (below)     → semantic/keyword search on the text.
    if image_bytes:
        mode = "image+caption" if text else "image-only"
        logger.info(f"Mode: {mode} | sender={sender}")
        profile = await state.openai_svc.analyze_image_profile(image_bytes, msg_type)

        # Embed the PURE image profile (style only) for cosine similarity search.
        # The caption text, if any, is applied afterwards as an explicit type filter
        # so we find items that MATCH THE STYLE of the photo and are of the right TYPE.
        query_vec: list[float] | None = None
        if profile:
            pure_embed = profile_to_embed_text(profile)
            query_vec = await state.openai_svc.embed_text(pure_embed)
            # Store context so a follow-up text message can blend this image's
            # style with a new type request ("matching jhumki tops").
            if query_vec:
                _set_sender_context(sender, pure_embed, query_vec)

        # 1. Stock code on the label AND in inventory → send the exact piece first,
        #    then follow it with up to 4 genuinely-similar pieces.
        stock = (profile.get("stock_number") or "").strip() if profile else ""
        if stock and state.ims.find_by_stock_number(stock):
            exact = await _send_exact(sender, stock, msg_id)
            if exact is not None and query_vec:
                similar = state.cache.find_semantic(
                    query_vec, k=MAX_SIMILAR_AFTER_EXACT + 14, min_score=settings.min_match_score
                )
                similar = [(it, sc) for it, sc in similar if it["id"] != exact["id"]]
                # If the caption names a different category ("I want matching bangles"),
                # filter similar suggestions to that type rather than the image's type.
                if text:
                    similar = _enforce_category(text, _enforce_weight(text, _enforce_length(text, similar)))
                similar = _relevant(similar)
                if similar:
                    await _send_matches(
                        sender, similar, None,
                        limit=MAX_SIMILAR_AFTER_EXACT, label_best=False, notify_if_empty=False,
                    )
            return

        # 2. No exact match → up to 5 best matches, trimmed to those that genuinely
        #    match (so only 2-3 are sent when only 2-3 are close).
        if query_vec:
            results = state.cache.find_semantic(
                query_vec, k=MAX_MATCHES + 14, min_score=settings.min_match_score
            )
            # When the caption names a jewelry type that differs from the image
            # (e.g. "show matching jhumki tops" sent with a necklace photo),
            # honour the requested type before the relevance trim so we don't
            # keep only the wrong category.
            if text:
                results = _enforce_category(text, _enforce_weight(text, _enforce_length(text, results)))
            results = _relevant(results)
            if results:
                state.cache.log_match(
                    sender, image_url or "",
                    [it for it, _ in results[:MAX_MATCHES]],
                    [sc or 0.0 for _, sc in results[:MAX_MATCHES]],
                )
                await _send_matches(sender, results, msg_id, limit=MAX_MATCHES)
                return

        # 3. Fallback — keyword search from text or profile category/colors
        fallback_q = text or " ".join((profile or {}).get("colors", []) + [(profile or {}).get("category", "")])
        matches = state.ims.find_by_color(fallback_q, limit=MAX_MATCHES)
        if matches:
            await _send_matches(sender, [(m, None) for m in matches], msg_id, limit=MAX_MATCHES)
            return

        await _reply_text(sender, "❌ Could not identify the item. Please send a clearer image or type the stock number.", msg_id)
        return

    # ── TEXT query ───────────────────────────────────────────────────────────
    query_type, query_value = await state.openai_svc.analyze_query(None, text, "text")
    logger.info(f"Text query analysis -> type={query_type} value={query_value}")

    if query_type == "stock":
        await _send_exact(sender, query_value, msg_id)
        return

    # color / describe → semantic search on the unified vector space.
    # If the customer sent an image recently, blend its style with the text
    # request so "matching jhumki tops" finds earrings that complement the image.
    raw_query = text or query_value or ""
    ctx = _get_sender_context(sender)
    if ctx:
        combined = f"{ctx['embed_text']} {raw_query}".strip()
        logger.info(f"Follow-up query — blending image context with: '{raw_query}'")
        # filter_query = original user text so category/length filters are NOT
        # confused by type words in the image embed text (e.g. "necklace" in the
        # profile would otherwise cancel out the "jhumki" earrings filter).
        results = await _semantic_search(combined, raw_query, filter_query=raw_query)
    else:
        results = await _semantic_search(raw_query, query_value or raw_query)
    if not results:
        await _reply_text(sender, f"❌ No matching items found for *{query_value or raw_query}*.", msg_id)
        return
    state.cache.log_match(
        sender, "",
        [it for it, _ in results],
        [sc or 0.0 for _, sc in results],
    )
    # typed query → show only the stock number + descriptor, no match %
    await _send_matches(sender, results, msg_id, show_score=False)


async def _semantic_search(
    raw_query: str,
    keyword_fallback: str,
    filter_query: str | None = None,
) -> list[tuple[dict, float | None]]:
    """Embedding search first, then keyword fallback. Returns (item, score) pairs.

    raw_query    — text that is embedded (may include image context for follow-ups).
    filter_query — text used for category/length/weight enforcement; defaults to
                   raw_query. Pass the original user text when raw_query is a
                   combined image+text string so category filters stay correct."""
    fq = filter_query if filter_query is not None else raw_query
    query_vec = await state.openai_svc.embed_text(raw_query)
    results = state.cache.find_semantic(query_vec, k=MAX_MATCHES + 7, min_score=settings.min_match_score)
    results = _enforce_category(fq, _enforce_weight(fq, _enforce_length(fq, results)))
    if results:
        logger.info(f"Semantic search for '{raw_query}' returned {len(results)} results")
        return results[:MAX_MATCHES]
    logger.info(f"Falling back to keyword search for '{keyword_fallback}'")
    fallback = [(m, None) for m in state.ims.find_by_color(keyword_fallback, limit=MAX_MATCHES + 7)]
    return _enforce_category(fq, _enforce_weight(fq, _enforce_length(fq, fallback)))[:MAX_MATCHES]


async def _send_exact(sender: str, stock: str, msg_id: str | None) -> dict | None:
    """Send the exact-match image. Returns the item when it was actually sent
    (available), or None when not found / reserved / unavailable (the customer is
    told why). The caller uses the return value to decide whether to follow up
    with similar pieces."""
    item = state.ims.find_by_stock_number(stock)
    if not item:
        await _reply_text(sender, f"❌ Item *{stock}* was not found in our inventory.", msg_id)
        return None
    available, remark = state.ims.check_availability(stock)
    if remark:
        await _reply_text(sender, f"❌ Item *{stock}* is already reserved.\n_{remark}_", msg_id)
        return None
    if not available:
        await _reply_text(sender, f"❌ Item *{stock}* is currently not available.", msg_id)
        return None
    filename = os.path.basename(item["local_path"])
    await asyncio.sleep(SEND_DELAY)
    await state.wa.send_image(sender, filename, f"✅ *{stock}* — exact match", quoted_msg_id=msg_id)
    return item


# Length words a customer might type, grouped into the buckets that a piece's
# profile["length"] uses. Plain cosine treats "long" vs "short" as a weak signal,
# so an explicit length in the query is enforced as a HARD filter instead.
_LONG_QUERY_WORDS = {"long", "opera", "layered", "haar", "rani"}
_SHORT_QUERY_WORDS = {"short", "choker", "collar"}
_LONG_PROFILE = {"long", "opera", "layered"}
_SHORT_PROFILE = {"short", "medium-length", "choker"}

# Query word → accepted profile categories (hard filter on semantic results).
# Covers all Drive folder names + common customer query words.
_TYPE_ALIASES: dict[str, set[str]] = {
    # earrings family
    "earrings": {"earrings"}, "earring": {"earrings"},
    "bali": {"earrings"}, "chand": {"earrings"},   # "chand bali"
    "chandbali": {"earrings"}, "jhumka": {"earrings"}, "jhumki": {"earrings"},
    "stud": {"earrings"}, "hoop": {"earrings"}, "tops": {"earrings"},
    "gutta": {"earrings"}, "pussal": {"earrings"},  # gutta pussal
    "kaan": {"earrings", "kaan"},
    # nose ring
    "nath": {"nath"},
    # tikka / forehead
    "tikka": {"tikka"}, "tika": {"tikka"}, "pika": {"tikka"},
    "maang-tikka": {"tikka"}, "matha": {"tikka"}, "patti": {"tikka"},
    # necklace family
    "necklace": {"necklace", "haar", "choker", "hasli"},
    "haar": {"necklace", "haar"},
    "choker": {"necklace", "choker"}, "chokar": {"necklace", "choker"},
    "hasli": {"hasli", "necklace"},
    "kashu": {"necklace"}, "mala": {"necklace", "haar"},
    "jalebi": {"necklace"}, "mini": {"necklace"},
    "mango": {"necklace", "haar"},
    # pendant
    "pendant": {"pendant"}, "magari": {"pendant"},
    # bangle / bracelet
    "bangle": {"bangle"}, "bracelet": {"bracelet"},
    # belt / kamarband
    "belt": {"kamarband"}, "kamarband": {"kamarband"},
    # hath phool (hand ornament)
    "hath": {"hath phool"}, "hathpool": {"hath phool"},
    # vanki (armlet)
    "vanki": {"vanki"},
    # others
    "ring": {"ring"}, "chain": {"chain"}, "set": {"set"},
}


def _enforce_category(query: str, results: list[tuple[dict, float | None]]) -> list[tuple[dict, float | None]]:
    """Drop results whose profile category doesn't match an explicit jewelry type
    in the query. Items with no recorded category are kept (unknown, not wrong).
    E.g. 'nakshi bali' → only earrings; 'nakshi belt' → only kamarband."""
    tokens = set(re.findall(r"[a-z]+", (query or "").lower()))
    accepted: set[str] = set()
    for token in tokens:
        if token in _TYPE_ALIASES:
            accepted |= _TYPE_ALIASES[token]
    if not accepted:
        return results
    kept = []
    for it, sc in results:
        category = (it.get("profile", {}).get("category") or "").lower()
        if not category or category in accepted:
            kept.append((it, sc))
    return kept


def _query_length_intent(query: str) -> str | None:
    """'long' / 'short' if the query explicitly asks for one, else None."""
    tokens = set(re.findall(r"[a-z]+", (query or "").lower()))
    if tokens & _LONG_QUERY_WORDS:
        return "long"
    if tokens & _SHORT_QUERY_WORDS:
        return "short"
    return None


def _enforce_length(query: str, results: list[tuple[dict, float | None]]) -> list[tuple[dict, float | None]]:
    """Drop results whose profiled length contradicts an explicit length in the
    query (e.g. asking for a 'long' necklace must never return a 'short' one).
    Pieces with no recorded length are kept — unknown, not contradicting."""
    intent = _query_length_intent(query)
    if not intent:
        return results
    banned = _SHORT_PROFILE if intent == "long" else _LONG_PROFILE
    kept = []
    for it, sc in results:
        length = (it.get("profile", {}).get("length") or "").lower()
        if length and length in banned:
            continue
        kept.append((it, sc))
    return kept


def _query_weight_range(query: str) -> tuple[float, float] | None:
    """Parse a weight constraint in grams from the query, e.g. 'between 35-37 grams',
    '35 to 37 gram', 'under 50g', 'over 40 grams', '40 gram'. Returns (min, max) in
    grams, or None when the query has no gram-based weight constraint."""
    q = (query or "").lower()
    if not re.search(r"\d\s*(?:g|gm|gms|gram|grams)\b", q):
        return None
    # explicit range: "35-37", "35 to 37", "35 and 37", "35 – 37"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|to|and)\s*(\d+(?:\.\d+)?)", q)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))
    # upper bound: "under / below / less than / up to / max 50"
    m = re.search(r"(?:under|below|less than|upto|up to|max|maximum)\s*(\d+(?:\.\d+)?)", q)
    if m:
        return (0.0, float(m.group(1)))
    # lower bound: "over / above / more than / at least / min 40"
    m = re.search(r"(?:over|above|more than|at least|min|minimum)\s*(\d+(?:\.\d+)?)", q)
    if m:
        return (float(m.group(1)), float("inf"))
    # single value: "40 grams" / "around 40 gram" → ±2g tolerance
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|gm|gms|gram|grams)\b", q)
    if m:
        v = float(m.group(1))
        return (v - 2.0, v + 2.0)
    return None


def _enforce_weight(query: str, results: list[tuple[dict, float | None]]) -> list[tuple[dict, float | None]]:
    """Keep only results whose net weight falls inside an explicit gram range in the
    query. When the query asks by weight, items with no recorded weight cannot be
    confirmed, so they are dropped."""
    rng = _query_weight_range(query)
    if not rng:
        return results
    lo, hi = rng
    kept = []
    for it, sc in results:
        w = _coerce_weight(it.get("profile", {}).get("weight"))
        if w is None:
            continue  # unknown weight can't satisfy an explicit weight query
        if lo <= w <= hi:
            kept.append((it, sc))
    return kept


def _coerce_weight(w) -> float | None:
    """Read a numeric gram value from a stored weight (number, '17.479', '17.479 gm')."""
    if w is None:
        return None
    if isinstance(w, (int, float)):
        return float(w)
    m = re.search(r"\d+(?:\.\d+)?", str(w))
    return float(m.group()) if m else None


def _relevant(results: list[tuple[dict, float]], gap: float = RELEVANCE_GAP) -> list[tuple[dict, float]]:
    """Keep only results close to the best hit (results come sorted best-first).

    This is what lets us send 2-3 images when only 2-3 genuinely match, instead of
    padding to 5 with loosely-related pieces. Scoreless results (None) always pass."""
    if not results:
        return []
    top = results[0][1]
    if top is None:
        return results
    return [(it, sc) for it, sc in results if sc is None or sc >= top - gap]


def _build_caption(item: dict, score: float | None, is_first: bool) -> str:
    """Compose a clean, human-readable WhatsApp caption for one matched item.

    Line 1: stock number + match confidence.
    Line 2: a short descriptor of the piece (metal / length / type)."""
    stock = item["name"].rsplit(".", 1)[0]
    desc = profile_caption(item.get("profile", {}))
    # fall back to the flat tags if the profile gives us nothing descriptive
    if not desc:
        tags = state.ims.tags_for(item)
        desc = ", ".join(tags[:4])

    if score is not None and is_first and score >= STRONG_MATCH:
        header = f"✅ *{stock}* — Best match"
    else:
        header = f"*{stock}*"

    return f"{header}\n{desc}" if desc else header


async def _send_matches(
    sender: str,
    results: list[tuple[dict, float | None]],
    msg_id: str | None,
    *,
    limit: int = MAX_MATCHES,
    label_best: bool = True,
    notify_if_empty: bool = True,
    show_score: bool = True,
):
    """Send up to `limit` AVAILABLE matches with confidence captions.

    results = (item, score|None), best-first. Unavailable/reserved items are
    skipped without counting toward the limit. label_best marks the first sent
    item as the "Best match" (off when these are the similar pieces that follow an
    exact match). notify_if_empty sends the "no matches" notice when nothing went
    out (off for the similar follow-up, where the exact match was already sent).
    show_score off hides the % entirely (used for typed/text queries — the customer
    sees only the stock number and the descriptor)."""
    sent = 0
    for item, score in results:
        if sent >= limit:
            break
        stock = item["name"].rsplit(".", 1)[0]
        available, remark = state.ims.check_availability(stock)
        if not available or remark:
            continue
        filename = os.path.basename(item["local_path"])
        caption = _build_caption(item, score if show_score else None, is_first=(label_best and sent == 0))

        await asyncio.sleep(SEND_DELAY)
        try:
            await state.wa.send_image(sender, filename, caption, quoted_msg_id=msg_id if sent == 0 else None)
            sent += 1
        except Exception as e:
            logger.error(f"Failed to send match: {e}")

    if sent == 0 and notify_if_empty:
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
