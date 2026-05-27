import asyncio
import logging
import os
import re

from fastapi import APIRouter, BackgroundTasks

import state
from config import settings
from services.cache_service import IMAGES_DIR

router = APIRouter(prefix="/admin")
logger = logging.getLogger(__name__)

_rebuild_running = False


@router.get("/status")
async def status():
    return {**state.cache.stats(), "rebuild_running": _rebuild_running}


@router.get("/matches")
async def matches(limit: int = 20):
    return {"matches": state.cache.match_history[:limit]}


@router.post("/refresh")
async def refresh(background: BackgroundTasks):
    global _rebuild_running
    if _rebuild_running:
        return {"status": "already_running"}
    background.add_task(_rebuild)
    return {"status": "started"}


@router.post("/rebuild-all")
async def rebuild_all(background: BackgroundTasks):
    """Rebuild Drive cache then build color index in one shot."""
    global _rebuild_running
    if _rebuild_running:
        return {"status": "already_running"}
    if not state.openai_svc:
        return {"status": "error", "reason": "OPENAI_API_KEY not set"}
    background.add_task(_rebuild_and_index)
    return {"status": "started"}


async def _rebuild():
    global _rebuild_running
    _rebuild_running = True
    loop = asyncio.get_event_loop()

    try:
        logger.info("Scanning Google Drive...")
        images = await loop.run_in_executor(
            None, state.drive.list_images, settings.drive_folder_id
        )
        logger.info(f"Found {len(images)} images in Drive")

        for img in images:
            filename = state.drive.safe_filename(img["id"], img["name"])
            dest = os.path.join(IMAGES_DIR, filename)

            # Skip if already cached (by file existence check)
            if os.path.exists(dest) and any(d["id"] == img["id"] for d in state.cache.images):
                logger.info(f"Already cached: {img['name']}")
                continue

            try:
                content = await loop.run_in_executor(
                    None, state.drive.download, img["id"], dest
                )
                embedding = await loop.run_in_executor(None, state.cnn.extract, content)
                state.cache.upsert(img["id"], img["name"], dest, embedding, img.get("folder_path", []))
                logger.info(f"Indexed: {img['name']}")
            except Exception as e:
                logger.error(f"Failed to process {img['name']}: {e}")

        state.cache.finalize()
        logger.info("Cache rebuild complete")
    except Exception as e:
        logger.error(f"Rebuild failed: {e}")
    finally:
        _rebuild_running = False


async def _rebuild_and_index():
    await _rebuild()
    logger.info("Starting color index after rebuild...")
    await _build_color_index()
    logger.info("Rebuild-all complete")


@router.post("/index-colors")
async def index_colors(background: BackgroundTasks):
    """Run GPT-4o-mini on all cached images to build the color index."""
    if not state.openai_svc:
        return {"status": "error", "reason": "OPENAI_API_KEY not set"}
    stats = state.ims.color_index_stats()
    if stats["pending"] == 0:
        return {"status": "already_complete", **stats}
    background.add_task(_build_color_index)
    return {"status": "started", **stats}


@router.get("/index-colors/status")
async def color_index_status():
    return state.ims.color_index_stats()


async def _build_color_index():
    logger.info("Building color index...")
    for item in state.cache.images:
        if item["name"] in state.ims._color_index:
            continue  # already indexed
        try:
            with open(item["local_path"], "rb") as f:
                img_bytes = f.read()
            colors = await state.openai_svc.extract_colors_from_image(img_bytes)
            # Merge GPT color tags with folder names (free labels from Drive structure)
            folder_tags = [f.lower() for f in item.get("folder_path", [])]
            all_tags = list(dict.fromkeys(colors + folder_tags))  # deduplicate, preserve order
            state.ims.update_color_index(item["name"], all_tags)
            logger.info(f"Color indexed: {item['name']} → {all_tags}")
            await asyncio.sleep(0.3)  # gentle rate limiting
        except Exception as e:
            logger.error(f"Color index failed for {item['name']}: {e}")
    logger.info("Color index complete")


@router.get("/catalog")
async def catalog():
    """Return all cached images with folder path and color tags for the dashboard."""
    items = []
    for img in state.cache.images:
        color_tags = state.ims._color_index.get(img["name"], []) if state.ims else []
        items.append({
            "id": img["id"],
            "name": img["name"],
            "stock": re.sub(r"\.[^.]+$", "", img["name"]),
            "folder_path": img.get("folder_path", []),
            "color_tags": color_tags,
            "image_url": f"/images/{os.path.basename(img['local_path'])}",
        })
    return {"items": items}


@router.delete("/cache")
async def clear_cache():
    """Wipe all cached embeddings and images (use before a full re-index)."""
    for item in state.cache.images:
        if os.path.exists(item["local_path"]):
            os.remove(item["local_path"])
    state.cache.images.clear()
    state.cache.last_updated = None
    state.cache._embedding_matrix = None
    state.cache.finalize()
    return {"status": "cleared"}
