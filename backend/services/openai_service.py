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
- Jewelry types: earrings, necklace, ring, bracelet, bangle, pendant, set, chain, nath, tikka, kaan, maang-tikka, pika, kamarband, hasli, hath phool, vanki, matha patti, gutta pussal, kashu mala, mango mala, jalebi necklace, magari pendant, tops, jhumki, chand bali, chokar, tika
- Aliases (use the mapped keyword):
  "pika" / "tika" = tikka | "belt" = kamarband | "bali" / "chand bali" / "chandbali" = earrings (chandbali subtype)
  "chokar" = choker (necklace) | "jhumki" / "jhumka" = earrings (jhumka subtype) | "tops" = earrings (stud subtype)
  "gutta pussal" = earrings | "hasli" = necklace (rigid torque) | "hath phool" / "hathpool" = hath phool (hand ornament)
  "vanki" = vanki (armlet) | "matha patti" / "maathapatti" / "mathaapatti" = tikka | "kashu mala" / "kashu" = necklace
  "mango mala" = necklace (haar) | "jalebi necklace" / "jalebi" = necklace | "magari pendant" / "magari" = pendant
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
  "stock_number": string | null,   // the item's stock code read from its paper tag — see "READING THE STOCK TAG" below. null only if no tag/code is visible.
  "weight": number | null,         // net weight in grams read from the paper tag — see "READING THE STOCK TAG" below. null if no weight is written.
  "description": string,           // one rich natural sentence describing the piece for search
  "category": string,              // ONE of: earrings, necklace, nath, tikka, ring, bracelet, bangle, pendant, set, kaan, chain, haar, choker, kamarband, hasli, hath phool, vanki
  "metal": string | null,          // ONE of: gold, silver, rose-gold, oxidized, two-tone
  "stone_color": string[],         // visible stone/enamel colors: red, blue, green, white, yellow, pink, purple, pearl, multicolor, emerald, ruby, sapphire
  "colors": string[],              // dominant overall colors of the piece (metal + stones combined)
  "subtype": string | null,        // jhumka, stud, hoop, chandbali, drop, choker, haar, layered, statement, cluster, matha patti
  "silhouette": string | null,     // the overall OUTLINE you would sketch: e.g. "row of tapering spikes", "round central medallion", "broad collar / bib", "single drop pendant", "V-shaped drop", "linear bar", "choker band", "teardrop cluster"
  "structure": string | null,      // how it is BUILT / arranged: e.g. "graduated spike danglers radiating from the centre", "central round pendant with hanging pearl tassels", "repeating identical articulated links", "multi-layer strands"
  "motif": string[],               // the repeating decorative SHAPES, every one you see: e.g. ["spike","kalgi","spear"], ["floral","medallion"], ["paisley"], ["peacock"], ["round"], ["square","kundan"], ["leaf"]
  "size": string | null,           // OVERALL SCALE of the piece: small, medium, large, mini, heavy
  "style": string[],               // traditional, modern, antique, kundan, meenakari, polki, temple, plain, filigree, bridal, casual, oxidized, jadau, nakshi
  "length": string | null,         // HOW FAR IT HANGS: short, medium-length, long, opera, layered
  "labels": string[],              // 3-6 short searchable labels, e.g. ["gold jhumka", "red stone earrings"]
  "tags": string[],                // 2-5 occasion/mood tags, e.g. ["festive", "wedding", "luxury"]
  "keywords": string[]             // 3-6 phrases a customer might type to find this, e.g. ["gold kundan jhumka", "red bridal earrings"]
}

Critical rules:
- A nath (nose ring) is NEVER earrings. A tikka/pika/matha patti (head/forehead ornament) is NEVER a necklace or earrings.
- A kamarband/belt (waist ornament) is NEVER a bangle or bracelet.
- A hasli (rigid torque necklace) → category "hasli". A hath phool / hathpool (hand ornament connecting ring to bracelet) → category "hath phool". A vanki (upper arm armlet) → category "vanki".
- Gutta pussal, jhumki, tops, chand bali → all earrings. Mango mala, kashu mala, jalebi necklace, hasli, mini necklace → all necklace or hasli. Matha patti → tikka.

MATHA PATTI vs CHOKER — this is the single most common misclassification:
- A MATHA PATTI (maang tikka / head ornament) has TWO parts: (1) a horizontal band of
  pearl strings, beads, or chains that sits across the FOREHEAD/HAIRLINE, and (2) a
  LONG VERTICAL TASSEL or chain that hangs DOWN from the CENTER of the band — this
  hanging piece is what the "patti" in the name refers to. When photographed on a
  display bust/mannequin, the horizontal band rests around the neck area, but the LONG
  DROP in the centre always identifies it as a matha patti. Category: "tikka".
- A CHOKER is worn around the NECK only. It does NOT have a long vertical pendant
  hanging from its centre — at most it has a small pendant pendant (shorter than
  the band width itself). No long drop = potential choker. Long central drop = matha patti.
- If you see a horizontal pearl/bead band AND a long dangling piece (even 3-4 cm) from
  its centre → category "tikka", subtype "matha patti".
- NEVER report the white/cream photo background as a color. "white" means white stones/enamel ON the jewelry.
- Only fill attributes you can clearly see; use null or [] when uncertain.
- "moti" = "pearl". Use lowercase everywhere.

READING THE STOCK TAG — most photos include a small white/cream OVAL paper tag tied
to the piece with a few handwritten lines. The stock code is the SINGLE most
important field, so read it carefully:
- The stock number is the line written as "S-<number>" or "S - <number>"
  (e.g. "S - 9417" → "9417",  "S-10350" → "10350"). Return just the code after "S".
- IGNORE every other number on the tag — they are NOT the stock number:
    * a circled number at the top-left (a batch/serial no.),
    * any "BS-<number>" line (that is a bill number),
    * any weight (a decimal like "14.86", often beside "gm").
- Read it even when the handwriting is messy. Only return null when there is
  genuinely no "S-" code visible anywhere.
- A code printed directly on the metal counts too.
- WEIGHT (grams): read the item's NET weight into "weight" as a number. Tags often
  show a breakdown — a gross weight (e.g. "70.65"), a pearl/stone weight (e.g.
  "P:14.582"), and a final UNDERLINED total at the BOTTOM (e.g. "56.068"). That
  underlined bottom total is the net weight — return it (56.068). If the tag shows
  only one weight number (e.g. "14.86 gm"), use that. null if no weight is written.

SHAPE & STRUCTURE — describe the actual FORM of the piece, not just its type. Two
gold necklaces can look completely different; "silhouette", "structure" and "motif"
are what tell them apart, so they MUST be filled whenever the form is visible:
- "silhouette" = the overall outline a person would sketch (spikes/rays, round
  medallion, broad collar/bib, single drop pendant, linear bar, choker band ...).
- "structure" = how it is constructed and arranged (graduated tapering danglers,
  central pendant with hanging tassels, repeating identical links, layered strands).
- "motif" = every repeating decorative shape you can see (spike / kalgi / spear,
  floral, paisley, peacock, round, square / kundan, leaf ...).
Be concrete and visual. These three fields ARE the design identity of the piece —
never leave them empty when the shape is visible, and never blur two different
shapes into the same generic words.

SIZE vs LENGTH — these are DIFFERENT and customers search by BOTH, do not confuse them:
- "size" = how big/heavy the piece looks overall (small / medium / large / mini / heavy).
- "length" = how far it hangs down (short / medium-length / long / opera / layered).
- ALWAYS set "length" for ANY hanging or elongated piece. This INCLUDES earrings —
  jhumka, chandbali, drop and dangling earrings all have a length. Also: necklace, haar,
  chain, choker, tikka, maang-tikka, kaan, and long pendants.
- EARRINGS length rule (judge by how far the earring drops below the earlobe):
    * stud / small button sitting on the lobe                → length "short"
    * normal jhumka / chandbali dropping a little            → length "medium-length"
    * long dangling earring, chain/drop hanging well down,
      multi-tier / pearl-string / shoulder-duster earrings   → length "long"
  An earring can be physically narrow/light but still LONG — set length "long" by how far
  it HANGS, never by how heavy it looks. Do NOT call a long earring "short" just because
  it is thin.
- A necklace example: a long haar or a tikka with a long chain → "long".
  A short choker sitting at the neck → "short".
- A piece can be BOTH (e.g. a big long haar → size "large" AND length "long"). Include both.
- When a customer types "long" they mean LENGTH — so a long earring or long haar must carry
  length "long", NOT just size "large". "large" ≠ "long". Never substitute one for the other.
"""


def _pdf_to_jpeg(pdf_bytes: bytes) -> bytes:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=150)
    return pix.tobytes("jpeg")


def profile_to_embed_text(profile: dict) -> str:
    """Flatten a Vision profile into the string we embed for semantic search.

    Shape/structure terms are REPEATED so they dominate the embedding: two pieces
    of the same category (e.g. "gold necklace") must be separated by their actual
    FORM, not by shared material / occasion words. Occasion tags carry the least
    visual signal, so they go last and unweighted. Folder names are appended by
    the caller (free labels from Drive structure)."""
    parts: list[str] = []
    if profile.get("description"):
        parts.append(str(profile["description"]))

    # ── Design identity (shape) — weighted 3× so FORM drives the match ───────
    shape_terms: list[str] = []
    for key in ("silhouette", "structure"):
        val = profile.get(key)
        if val:
            shape_terms.append(str(val))
    for v in profile.get("motif") or []:
        if v:
            shape_terms.append(str(v))
    if shape_terms:
        parts.extend([" ".join(shape_terms)] * 3)

    # ── Type — weighted 2× ───────────────────────────────────────────────────
    for key in ("category", "subtype"):
        val = profile.get(key)
        if val:
            parts.extend([str(val)] * 2)

    # ── Plain attributes — single weight ─────────────────────────────────────
    for key in ("metal", "size", "length"):
        val = profile.get(key)
        if val:
            parts.append(str(val))
    for key in ("colors", "stone_color", "style", "labels", "keywords"):
        vals = profile.get(key) or []
        if isinstance(vals, list):
            parts.extend(str(v) for v in vals if v)

    # ── Occasion / mood tags last — least relevant to a visual match ─────────
    for v in profile.get("tags") or []:
        if v:
            parts.append(str(v))
    return " ".join(parts).strip()


def profile_caption(profile: dict) -> str:
    """Build a short human-readable descriptor of a piece for a WhatsApp caption.

    Reads like a salesperson would describe it, e.g. "Gold long chandbali earrings"
    or "Pearl traditional necklace". Order: metal → length → subtype/style → category.
    Falls back gracefully when attributes are missing."""
    if not profile:
        return ""

    metal = (profile.get("metal") or "").strip()
    category = (profile.get("category") or "").strip()
    subtype = (profile.get("subtype") or "").strip()
    # length describes how far it hangs; only show meaningful values (skip "medium-length")
    length = (profile.get("length") or "").strip()
    length = length if length in {"long", "short", "opera", "layered"} else ""
    # if no length, fall back to overall size so the customer still gets a scale hint
    size = (profile.get("size") or "").strip()
    # the dominant motif makes the descriptor concrete ("spike" vs generic "statement")
    motifs = profile.get("motif") or []
    motif = str(motifs[0]).strip() if motifs else ""

    parts: list[str] = []
    if metal:
        parts.append(metal)
    if length:
        parts.append(length)
    elif size and size not in {"medium"}:
        parts.append(size)
    if motif:
        parts.append(motif)
    if subtype and subtype != "statement":
        parts.append(subtype)
    if category:
        parts.append(category)

    # de-dupe while preserving order (e.g. subtype == category)
    parts = list(dict.fromkeys(p for p in parts if p))
    if not parts:
        return ""
    phrase = " ".join(parts)
    return phrase[:1].upper() + phrase[1:]


def profile_tags(profile: dict) -> list[str]:
    """Flat lowercase tag list used by keyword/color fallback search."""
    tags: list[str] = []
    for key in ("category", "metal", "subtype", "size", "length"):
        val = profile.get(key)
        if val:
            tags.append(str(val).lower())
    for key in ("colors", "stone_color", "style", "labels", "tags", "motif"):
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
