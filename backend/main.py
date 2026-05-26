import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import state
from config import settings
from models.cnn_extractor import CNNExtractor
from routers import admin, guru_kripa, webhook
from services.cache_service import CacheService
from services.drive_service import DriveService
from services.ims_service import IMSService
from services.openai_service import OpenAIService
from services.whatsapp_service import WhatsAppService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="WA Image Matcher", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    logger.info("Initialising services...")
    state.cnn = CNNExtractor()
    state.cache = CacheService()
    state.drive = DriveService(settings.google_credentials_path)
    state.wa = WhatsAppService(settings.wa_api_url, settings.wa_api_key, settings.backend_url)
    state.ims = IMSService(state.cache)
    if settings.openai_api_key:
        state.openai_svc = OpenAIService(settings.openai_api_key)
        logger.info("OpenAI service ready")
    else:
        logger.warning("OPENAI_API_KEY not set — Guru Kripa module disabled")
    logger.info("All services ready")


os.makedirs("cache/images", exist_ok=True)
app.mount("/images", StaticFiles(directory="cache/images"), name="images")

app.include_router(webhook.router)
app.include_router(admin.router)
app.include_router(guru_kripa.router)


@app.get("/")
async def root():
    return {"status": "ok", **(state.cache.stats() if state.cache else {})}
