import asyncio
import logging
import os
import re

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends

import state
from config import settings
from core.security import require_admin
from services.cache_service import IMAGES_DIR
from services.openai_service import profile_to_embed_text

router = APIRouter(prefix="/admin")
logger = logging.getLogger(__name__)

# Lock guards the indexing pass — replaces the racy boolean flag (BUG-06).
_index_lock = asyncio.Lock()


async def auto_index_loop(interval_minutes: int):
    """Background task (started from the app lifespan): every interval_minutes,
    scan Drive and index any NEW images. Incremental — already-profiled images
    are skipped, so an idle run costs nothing beyond the Drive listing."""
    if interval_minutes <= 0:
        logger.info("Auto-index disabled (AUTO_INDEX_INTERVAL_MINUTES=0)")
        return
    logger.info(f"Auto-index enabled — scanning Drive every {interval_minutes} min")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        if not state.openai_svc or _index_lock.locked():
            continue
        try:
            logger.info("Auto-index: checking Drive for new images...")
            await _index(None, False)
        except Exception as e:
            logger.error(f"Auto-index run failed: {e}")


@router.get("/status")
async def status():
    return {**state.cache.stats(), "index_running": _index_lock.locked()}


@router.get("/index/status")
async def index_status():
    return {**state.cache.profile_stats(), "index_running": _index_lock.locked()}


@router.get("/matches")
async def matches(limit: int = 20):
    return {"matches": state.cache.match_history[:limit]}


@router.post("/refresh", dependencies=[Depends(require_admin)])
async def refresh(background: BackgroundTasks):
    """Full index: scan Drive, download + Vision-profile + embed every image that
    isn't profiled yet."""
    if not state.openai_svc:
        return {"status": "error", "reason": "OPENAI_API_KEY not set"}
    if _index_lock.locked():
        return {"status": "already_running"}
    background.add_task(_index, None, False)
    return {"status": "started", **state.cache.profile_stats()}


@router.post("/reindex", dependencies=[Depends(require_admin)])
async def reindex(background: BackgroundTasks, limit: int = 30, force: bool = False):
    """Test-batch tool: profile up to `limit` images so you can eyeball quality in
    /admin/catalog before running the full /admin/refresh. force=true re-profiles
    already-indexed images (use to re-test after prompt changes)."""
    if not state.openai_svc:
        return {"status": "error", "reason": "OPENAI_API_KEY not set"}
    if _index_lock.locked():
        return {"status": "already_running"}
    background.add_task(_index, limit, force)
    return {"status": "started", "limit": limit, "force": force, **state.cache.profile_stats()}


async def _index(limit: int | None, force: bool):
    """Scan Drive and build Vision profile + embedding for each image.
    limit caps how many images are *processed* this run (for cheap test batches).
    force re-profiles images that already have a profile."""
    if _index_lock.locked():
        logger.info("Index already running — skipping duplicate run")
        return
    loop = asyncio.get_event_loop()
    processed = 0

    await _index_lock.acquire()
    try:
        logger.info("Scanning Google Drive...")
        images = await loop.run_in_executor(None, state.drive.list_images, settings.drive_folder_id)
        logger.info(f"Found {len(images)} images in Drive")

        existing = {d["id"]: d for d in state.cache.images}

        for img in images:
            if limit is not None and processed >= limit:
                logger.info(f"Reached limit of {limit} — stopping")
                break

            already = existing.get(img["id"])
            has_profile = bool(already and already.get("profile") and already.get("embedding") is not None)
            if has_profile and not force:
                continue

            filename = state.drive.safe_filename(img["id"], img["name"])
            dest = os.path.join(IMAGES_DIR, filename)

            try:
                # Download if we don't already have the bytes on the volume
                if os.path.exists(dest):
                    with open(dest, "rb") as f:
                        content = f.read()
                else:
                    content = await loop.run_in_executor(None, state.drive.download, img["id"], dest)

                profile = await state.openai_svc.analyze_image_profile(content)
                if not profile:
                    logger.warning(f"No profile for {img['name']} — skipped")
                    continue

                folder_path = img.get("folder_path", [])
                stock = re.sub(r"\.[^.]+$", "", img["name"])
                embed_text = " ".join([
                    profile_to_embed_text(profile),
                    " ".join(folder_path),
                    stock,
                ]).strip()
                embedding = await state.openai_svc.embed_text(embed_text)
                if not embedding:
                    logger.warning(f"No embedding for {img['name']} — skipped")
                    continue

                state.cache.upsert(
                    img["id"], img["name"], dest, folder_path,
                    profile=profile, embedding=np.asarray(embedding, dtype=np.float32),
                )
                processed += 1
                logger.info(f"Indexed [{processed}]: {img['name']} → {profile.get('category')} {profile.get('colors')}")
                await asyncio.sleep(0.2)  # gentle rate limiting
            except Exception as e:
                logger.error(f"Failed to index {img['name']}: {e}")

        state.cache.finalize()
        logger.info(f"Index complete — processed {processed} images this run")
    except Exception as e:
        logger.error(f"Index failed: {e}")
    finally:
        _index_lock.release()


@router.get("/catalog")
async def catalog():
    """All cached images with profile + folder path for the dashboard."""
    items = []
    for img in state.cache.images:
        profile = img.get("profile", {}) or {}
        items.append({
            "id": img["id"],
            "name": img["name"],
            "stock": re.sub(r"\.[^.]+$", "", img["name"]),
            "folder_path": img.get("folder_path", []),
            "category": profile.get("category"),
            "colors": profile.get("colors", []),
            "description": profile.get("description"),
            "tags": state.ims.tags_for(img) if state.ims else [],
            "profiled": img.get("embedding") is not None,
            "image_url": f"/images/{os.path.basename(img['local_path'])}",
        })
    return {"items": items}


@router.delete("/cache", dependencies=[Depends(require_admin)])
async def clear_cache():
    """Wipe all cached profiles, embeddings and images (use before a full re-index)."""
    for item in state.cache.images:
        if os.path.exists(item["local_path"]):
            os.remove(item["local_path"])
    state.cache.images.clear()
    state.cache.last_updated = None
    state.cache.finalize()
    return {"status": "cleared"}
