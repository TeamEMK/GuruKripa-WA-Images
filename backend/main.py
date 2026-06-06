import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import state
from config import settings
from routers import admin, guru_kripa, webhook
from services.cache_service import CacheService
from services.drive_service import DriveService
from services.ims_service import IMSService
from services.openai_service import OpenAIService
from services.whatsapp_service import WhatsAppService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


def _write_credentials():
    """Write credentials.json from base64 env var if file doesn't exist (Railway)."""
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_BASE64", "")
    if creds_b64 and not os.path.exists(settings.google_credentials_path):
        with open(settings.google_credentials_path, "wb") as f:
            f.write(base64.b64decode(creds_b64))
        logger.info("credentials.json written from GOOGLE_CREDENTIALS_BASE64")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initialising services...")
    _write_credentials()
    state.cache = CacheService()
    state.drive = DriveService(settings.google_credentials_path)
    state.wa = WhatsAppService(settings.wa_api_url, settings.wa_api_key, settings.backend_url)
    state.ims = IMSService(state.cache)
    if settings.openai_api_key:
        state.openai_svc = OpenAIService(
            settings.openai_api_key,
            embedding_model=settings.embedding_model,
            vision_model=settings.vision_model,
        )
        logger.info(f"OpenAI ready (vision={settings.vision_model}, embed={settings.embedding_model})")
    else:
        logger.warning("OPENAI_API_KEY not set — image/text search disabled")
    if not settings.admin_api_key:
        logger.warning("ADMIN_API_KEY not set — admin/IMS endpoints are UNPROTECTED")
    await guru_kripa.ensure_worker_started()
    if settings.openai_api_key:
        asyncio.create_task(admin.auto_index_loop(settings.auto_index_interval_minutes))
    logger.info("All services ready")
    yield


app = FastAPI(title="WA Image Matcher", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("cache/images", exist_ok=True)
app.mount("/images", StaticFiles(directory="cache/images"), name="images")

app.include_router(webhook.router)
app.include_router(admin.router)
app.include_router(guru_kripa.router)


@app.get("/")
async def root():
    return {"status": "ok", **(state.cache.stats() if state.cache else {})}


@app.get("/health")
async def health():
    """Deeper healthcheck — reports dependency readiness (not just process up)."""
    return {
        "status": "ok",
        "openai_ready": state.openai_svc is not None,
        "drive_ready": state.drive is not None,
        "admin_protected": bool(settings.admin_api_key),
        **(state.cache.profile_stats() if state.cache else {}),
    }
