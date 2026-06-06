import base64
import json
import logging

import httpx

logger = logging.getLogger(__name__)

# ── Query analysis (text/label → stock number or descriptive keywords) ───────
SYSTEM_PROMPT = """You are an inventory assistant for a jewelry business.
Analyze the customer's image and/or message and return ONE of these formats:

1. If a stock number / item code is visible or mentioned:
   STOCK:<code>
   Examples: STOCK:9858  or  STOCK:S-1655

2. If the customer describes what they want (color, type, size, style, shape):
   COLOR:<keywords>
   Extract ALL descriptive keywords from the request, comma-separated.
   Examples:
     COLOR:red,earrings
     COLOR:gold,necklace,traditional
     COLOR:silver,jhumka,large
     COLOR:nath,small
     COLOR:kundan,earrings
     COLOR:oxidized,necklace,antique
     COLOR:gold,tikka,bridal

3. If you cannot determine either:
   NOT_FOUND

Rules:
- Return ONLY one of the three formats above - nothing else
- Stock numbers look like: 9858, 10468, S-1655, S-1387 (numeric or alphanumeric)
- Jewelry types: earrings, necklace, ring, bracelet, bangle, pendant, set, chain, nath, tikka, kaan, maang-tikka
- Subtypes: jhumka, stud, hoop, chandbali, drop, choker, haar, layered, long, short
- Sizes: small, medium, large, mini, heavy, statement
- Necklace lengths: short, medium-length, long, opera, layered
- Styles: traditional, modern, antique, kundan, meenakari, polki, temple, oxidized, plain, bridal, casual
- Colors: gold, silver, rose-gold, white, black, red, green, blue, pink, yellow, oxidized, multicolor, pearl
- Do not guess stock numbers - only return STOCK: if you are certain
- When analyzing an IMAGE query: identify the dominant material first (pearl/moti = include "pearl"; mostly metal = include "gold" or "silver")
- "moti" and "pearl" are the same - use "pearl"
"""

# ── Image profiling (full structured attributes for indexing + search) ───────
# This replaces the CNN visual embedding. Every image is described by a rich
# jewelry-tuned attribute profile; the profile text is then embedded so that
# image→image and text→image search share one vector space.
PROFILE_PROMPT = """You are an expert Indian jewelry classifier. Analyze ONLY the jewelry item in the image - ignore the white/plain photo background completely.

Return a SINGLE JSON object (no markdown, no commentary) with exactly these keys:

{
  "stock_number": string | null,   // ONLY if a code/tag is clearly printed on the item or a label, e.g. "9858", "S-1655". Never guess.
  "description": string,           // one rich natural sentence describing the piece for search
  "category": string,              // ONE of: earrings, necklace, nath, tikka, ring, bracelet, bangle, pendant, set, kaan, chain, haar, choker
  "metal": string | null,          // ONE of: gold, silver, rose-gold, oxidized, two-tone
  "stone_color": string[],         // visible stone/enamel colors: red, blue, green, white, yellow, pink, purple, pearl, multicolor, emerald, ruby, sapphire
  "colors": string[],              // dominant overall colors of the piece (metal + stones combined)
  "subtype": string | null,        // jhumka, stud, hoop, chandbali, drop, choker, haar, layered, statement, cluster
  "size": string | null,           // small, medium, large, mini, heavy
  "style": string[],               // traditional, modern, antique, kundan, meenakari, polki, temple, plain, filigree, bridal, casual, oxidized, jadau, nakshi
  "length": string | null,         // necklaces only: short, medium-length, long, opera, layered
  "labels": string[],              // 3-6 short searchable labels, e.g. ["gold jhumka", "red stone earrings"]
  "tags": string[],                // 2-5 occasion/mood tags, e.g. ["festive", "wedding", "luxury"]
  "keywords": string[]             // 3-6 phrases a customer might type to find this, e.g. ["gold kundan jhumka", "red bridal earrings"]
}

Critical rules:
- A nath (nose ring) is NEVER earrings. A tikka (forehead piece) is NEVER a necklace.
- NEVER report the white/cream photo background as a color. "white" means white stones/enamel ON the jewelry.
- Only fill attributes you can clearly see; use null or [] when uncertain.
- "moti" = "pearl". Use lowercase everywhere.
"""


def _pdf_to_jpeg(pdf_bytes: bytes) -> bytes:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=150)
    return pix.tobytes("jpeg")


def profile_to_embed_text(profile: dict) -> str:
    """Flatten a Vision profile into the string we embed for semantic search.
    Folder names are appended by the caller (free labels from Drive structure)."""
    parts: list[str] = []
    if profile.get("description"):
        parts.append(str(profile["description"]))
    for key in ("category", "metal", "subtype", "size", "length"):
        val = profile.get(key)
        if val:
            parts.append(str(val))
    for key in ("colors", "stone_color", "style", "labels", "tags", "keywords"):
        vals = profile.get(key) or []
        if isinstance(vals, list):
            parts.extend(str(v) for v in vals if v)
    return " ".join(parts).strip()


def profile_tags(profile: dict) -> list[str]:
    """Flat lowercase tag list used by keyword/color fallback search."""
    tags: list[str] = []
    for key in ("category", "metal", "subtype", "size", "length"):
        val = profile.get(key)
        if val:
            tags.append(str(val).lower())
    for key in ("colors", "stone_color", "style", "labels", "tags"):
        for v in profile.get(key) or []:
            if v:
                tags.append(str(v).lower())
    # de-dupe, preserve order
    return list(dict.fromkeys(tags))


class OpenAIService:
    def __init__(self, api_key: str, embedding_model: str = "text-embedding-3-large", vision_model: str = "gpt-4.1"):
        self._api_key = api_key
        self._embedding_model = embedding_model
        self._vision_model = vision_model

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def analyze_query(
        self,
        image_bytes: bytes | None,
        text: str | None,
        media_type: str = "image",
    ) -> tuple[str, str | None]:
        """Returns (query_type, value) where query_type is 'stock', 'color', or 'not_found'."""
        content: list[dict] = []

        if text:
            content.append({"type": "text", "text": f"Customer message: {text}"})

        if image_bytes:
            if media_type == "document":
                try:
                    image_bytes = _pdf_to_jpeg(image_bytes)
                except Exception as e:
                    logger.warning(f"PDF conversion failed: {e}")
                    image_bytes = None

            if image_bytes:
                b64 = base64.b64encode(image_bytes).decode()
                content.append({
                    "type": "image_url",
                    # "low" detail — this path only classifies (stock vs describe);
                    # rich attribute extraction uses analyze_image_profile (high).
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                })

        if not content:
            return "not_found", None

        content.append({"type": "text", "text": "Analyze and respond in the exact format specified."})

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": self._vision_model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": content},
                        ],
                        "max_tokens": 30,
                        "temperature": 0,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                logger.info(f"{self._vision_model} query analysis: '{raw}'")

                if raw.startswith("STOCK:"):
                    return "stock", raw[6:].strip()
                if raw.startswith("COLOR:"):
                    return "color", raw[6:].strip().lower()
                return "not_found", None
        except Exception as e:
            logger.error(f"OpenAI analyze_query failed: {e}")
            return "not_found", None

    async def analyze_image_profile(self, image_bytes: bytes, media_type: str = "image") -> dict:
        """Full jewelry-tuned attribute profile for an image. Used both during
        indexing (to build the searchable embedding) and at query time (to embed
        an incoming image into the same vector space). Returns {} on failure."""
        if media_type == "document":
            try:
                image_bytes = _pdf_to_jpeg(image_bytes)
            except Exception as e:
                logger.warning(f"PDF conversion failed: {e}")
                return {}

        b64 = base64.b64encode(image_bytes).decode()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": self._vision_model,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "user", "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
                                {"type": "text", "text": PROFILE_PROMPT},
                            ]},
                        ],
                        "max_tokens": 500,
                        "temperature": 0,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                profile = json.loads(raw)
                if not isinstance(profile, dict):
                    logger.error(f"Profile JSON not an object: {raw[:200]}")
                    return {}
                logger.info(f"Profile: {profile.get('category')} / {profile.get('colors')} / stock={profile.get('stock_number')}")
                return profile
        except Exception as e:
            logger.error(f"analyze_image_profile failed: {e}")
            return {}

    async def embed_text(self, text: str) -> list[float]:
        """Generate a semantic embedding for text. Model is configurable
        (default text-embedding-3-large → 3072-d)."""
        if not text or not text.strip():
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers=self._headers(),
                    json={"model": self._embedding_model, "input": text},
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"embed_text failed: {e}")
            return []
