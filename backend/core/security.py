import logging
from urllib.parse import urlparse

from fastapi import Header, HTTPException

from config import settings

logger = logging.getLogger(__name__)


def require_admin(x_admin_key: str | None = Header(default=None)):
    """Dependency guarding mutating admin/IMS routes.

    - If ADMIN_API_KEY is configured, the X-Admin-Key header must match.
    - If it is NOT configured, access is allowed but a warning is logged so the
      gap is visible. Set ADMIN_API_KEY (and send it from the dashboard) in any
      shared/production environment.
    """
    if not settings.admin_api_key:
        logger.warning("ADMIN_API_KEY is not set — admin endpoints are UNPROTECTED")
        return
    if not x_admin_key or x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Key")


def media_host_allowed(url: str) -> bool:
    """SSRF guard: only allow downloads from explicitly trusted media hosts.
    Empty allowlist (no wa_api_url, no override) → allow but warn."""
    hosts = settings.media_hosts
    if not hosts:
        logger.warning("No media host allowlist configured — downloading %s unchecked", url)
        return True
    host = (urlparse(url).hostname or "").lower()
    allowed = host in hosts
    if not allowed:
        logger.warning("Blocked media download from untrusted host: %s", host)
    return allowed
