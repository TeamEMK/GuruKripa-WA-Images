from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" → leftover/legacy keys in .env or the Railway environment
    # never crash startup (Railway also injects PORT, RAILWAY_* etc.).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── WhatsApp (aumpfy) ────────────────────────────────────
    wa_api_key: str
    wa_api_url: str = ""          # send-image endpoint
    wa_text_api_url: str = ""     # send-text endpoint
    wa_images_group_id: str = ""
    webhook_secret: str = ""

    # ── Google Drive ─────────────────────────────────────────
    google_credentials_path: str = "credentials.json"
    drive_folder_id: str

    # ── Server ───────────────────────────────────────────────
    backend_url: str = "http://localhost:8000"

    # ── OpenAI ───────────────────────────────────────────────
    openai_api_key: str = ""
    vision_model: str = "gpt-4.1"
    embedding_model: str = "text-embedding-3-large"

    # ── Guru Kripa (WhatsApp group) ──────────────────────────
    guru_kripa_group_id: str = ""
    guru_kripa_webhook_secret: str = ""


settings = Settings()
