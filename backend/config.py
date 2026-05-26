from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── WA Images ────────────────────────────────────────────
    wa_api_key: str
    wa_api_url: str = ""
    wa_text_url: str = ""
    wa_images_group_id: str = ""
    webhook_secret: str = ""

    # ── Google Drive ─────────────────────────────────────────
    google_credentials_path: str = "credentials.json"
    drive_folder_id: str

    # ── Server ───────────────────────────────────────────────
    backend_url: str = "http://localhost:8000"
    cache_dir: str = "cache"

    # ── Guru Kripa ───────────────────────────────────────────
    openai_api_key: str = ""
    guru_kripa_group_id: str = ""
    guru_kripa_webhook_secret: str = ""
    wa_text_api_url: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
