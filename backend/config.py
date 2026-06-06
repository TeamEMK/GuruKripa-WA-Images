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

    # ── Security ─────────────────────────────────────────────
    # If set, all mutating /admin and /guru-kripa/ims routes require the
    # X-Admin-Key header. Leave empty ONLY for local dev (a loud warning is logged).
    admin_api_key: str = ""
    # Comma-separated allowed CORS origins. "*" allows any (not recommended in prod).
    allowed_origins: str = "*"
    # Comma-separated hostnames the server may download media from. Empty → derived
    # from wa_api_url. Blocks SSRF via forged webhook mediaUrl.
    media_allowed_hosts: str = ""

    # ── OpenAI ───────────────────────────────────────────────
    openai_api_key: str = ""
    vision_model: str = "gpt-4.1"
    embedding_model: str = "text-embedding-3-large"
    # Drop semantic matches below this cosine score (0.0 = keep all). Tune after
    # eyeballing results — ~0.2 trims clearly-irrelevant top-5 padding.
    min_match_score: float = 0.0

    # ── Auto-index ───────────────────────────────────────────
    # Periodically scan Drive and index any NEW images (incremental; already-
    # profiled images are skipped, so it only costs OpenAI for new files).
    # 0 = disabled. Runs on the single instance.
    auto_index_interval_minutes: int = 10

    # ── Guru Kripa (WhatsApp group) ──────────────────────────
    guru_kripa_group_id: str = ""
    guru_kripa_webhook_secret: str = ""

    # ── Derived helpers ──────────────────────────────────────
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()] or ["*"]

    @property
    def media_hosts(self) -> set[str]:
        hosts = {h.strip().lower() for h in self.media_allowed_hosts.split(",") if h.strip()}
        if not hosts and self.wa_api_url:
            from urllib.parse import urlparse
            host = urlparse(self.wa_api_url).hostname
            if host:
                hosts.add(host.lower())
        return hosts


settings = Settings()
