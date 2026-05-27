import base64
import logging

import httpx

logger = logging.getLogger(__name__)

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
- Return ONLY one of the three formats above — nothing else
- Stock numbers look like: 9858, 10468, S-1655, S-1387 (numeric or alphanumeric)
- Jewelry types: earrings, necklace, ring, bracelet, bangle, pendant, set, chain, nath, tikka, kaan, maang-tikka
- Subtypes: jhumka, stud, hoop, chandbali, drop, choker, haar, layered
- Sizes: small, medium, large, mini, heavy, statement
- Styles: traditional, modern, antique, kundan, meenakari, polki, temple, oxidized, plain, bridal, casual
- Colors: gold, silver, rose-gold, white, black, red, green, blue, pink, yellow, oxidized, multicolor, pearl
- Do not guess stock numbers — only return STOCK: if you are certain"""


COLOR_INDEX_PROMPT = """You are an expert Indian jewelry classifier. Analyze ONLY the jewelry item in the image — completely ignore the white/plain background.

Extract 4-8 descriptive tags covering these attributes:

METAL (pick one): gold | silver | rose-gold | oxidized | two-tone
STONE COLOR (if stones present): red | blue | green | white | yellow | pink | purple | pearl | multicolor | emerald | ruby | sapphire
TYPE (pick one — be very precise):
  - earrings: any ear jewelry (jhumka, stud, hoop, chandbali, drop, dangler)
  - necklace: neck jewelry (chain, haar, choker, layered, mangalsutra)
  - nath: nose ring worn on nose — NEVER confuse with earrings
  - tikka / maang-tikka: forehead piece with chain — NEVER confuse with necklace
  - ring: finger ring
  - bracelet: flexible wrist jewelry
  - bangle: rigid wrist ring
  - pendant: charm without chain
  - set: 2+ matching pieces together
  - kaan: ear-to-hair chain
SUBTYPE (if identifiable): jhumka | stud | hoop | chandbali | drop | choker | haar | layered | statement | cluster
SIZE: small | medium | large | mini | heavy
STYLE: traditional | modern | antique | kundan | meenakari | polki | temple | plain | filigree | bridal | casual | oxidized

Critical rules:
- NEVER tag white/cream photo background as "white" — white means white stones/enamel on the jewelry itself
- A nath (nose ring) is NOT earrings
- A tikka (forehead) is NOT a necklace
- Only tag attributes you can clearly see — omit if uncertain
- Return ONLY lowercase comma-separated tags, no labels or explanations
- Good examples:
  "gold,earrings,jhumka,red,large,traditional"
  "silver,necklace,choker,blue,kundan"
  "gold,nath,small,plain"
  "oxidized,earrings,chandbali,multicolor,statement,antique"
  "gold,tikka,pearl,bridal"
  "gold,earrings,stud,small,modern"


def _pdf_to_jpeg(pdf_bytes: bytes) -> bytes:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=150)
    return pix.tobytes("jpeg")


class OpenAIService:
    def __init__(self, api_key: str):
        self._api_key = api_key

    async def analyze_query(
        self,
        image_bytes: bytes | None,
        text: str | None,
        media_type: str = "image",
    ) -> tuple[str, str | None]:
        """
        Returns (query_type, value) where query_type is 'stock', 'color', or 'not_found'.
        """
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
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
                })

        if not content:
            return "not_found", None

        content.append({"type": "text", "text": "Analyze and respond in the exact format specified."})

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4.1",
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
                logger.info(f"GPT-4o query analysis: '{raw}'")

                if raw.startswith("STOCK:"):
                    return "stock", raw[6:].strip()
                if raw.startswith("COLOR:"):
                    return "color", raw[6:].strip().lower()
                return "not_found", None
        except Exception as e:
            logger.error(f"OpenAI analyze_query failed: {e}")
            return "not_found", None

    async def extract_colors_from_image(self, image_bytes: bytes) -> list[str]:
        """Used during cache indexing — extracts color tags from a product image."""
        b64 = base64.b64encode(image_bytes).decode()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4.1-mini",  # cheaper model for bulk indexing
                        "messages": [
                            {"role": "user", "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
                                {"type": "text", "text": COLOR_INDEX_PROMPT},
                            ]},
                        ],
                        "max_tokens": 60,
                        "temperature": 0,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip().lower()
                colors = [c.strip() for c in raw.split(",") if c.strip()]
                logger.info(f"Color tags extracted: {colors}")
                return colors
        except Exception as e:
            logger.error(f"Color extraction failed: {e}")
            return []

    # Keep backwards compat for any existing callers
    async def extract_stock_number(self, image_bytes, text, media_type="image"):
        qtype, value = await self.analyze_query(image_bytes, text, media_type)
        return value if qtype == "stock" else None
