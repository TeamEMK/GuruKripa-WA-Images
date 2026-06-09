import json
import logging
import os
import re

from services.openai_service import profile_tags

logger = logging.getLogger(__name__)

REMARKS_FILE = "cache/ims_remarks.json"


class IMSService:
    """Stock lookup, availability/reservation remarks, and keyword/color fallback
    search. Semantic (embedding) search lives in CacheService now — this service
    only handles exact-code lookup, availability, and the lexical fallback used
    when the vector index is empty or returns nothing."""

    def __init__(self, cache_service):
        self.cache = cache_service
        self._remarks: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(REMARKS_FILE):
            with open(REMARKS_FILE) as f:
                self._remarks = json.load(f)
            logger.info(f"Loaded {len(self._remarks)} IMS remarks")

    def _save(self):
        os.makedirs("cache", exist_ok=True)
        tmp = f"{REMARKS_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(self._remarks, f, indent=2)
        os.replace(tmp, REMARKS_FILE)  # atomic — no corruption on crash

    # ── Tags ─────────────────────────────────────────────────────────────────
    @staticmethod
    def tags_for(item: dict) -> list[str]:
        """Flat lowercase tag list for an indexed image (profile attrs + folders)."""
        tags = profile_tags(item.get("profile", {}))
        folders = [f.lower() for f in item.get("folder_path", [])]
        return list(dict.fromkeys(tags + folders))

    # ── Stock lookup ─────────────────────────────────────────────────────────
    def find_by_stock_number(self, stock_number: str) -> dict | None:
        """Search cached Drive images for a filename that matches the stock number.

        Exact (whole-name) match wins. Otherwise a token match — the query must
        equal a discrete token in the filename (so "Copy of 9858" → 9858), NOT a
        loose substring (which let "165" wrongly match "1655")."""
        query = stock_number.strip().upper()
        if not query:
            return None
        for item in self.cache.images:
            name_no_ext = re.sub(r"\.[^.]+$", "", item["name"].upper())
            if query == name_no_ext:
                return item
        # Token match: split the filename on non-alphanumerics, require exact token equality
        for item in self.cache.images:
            name_no_ext = re.sub(r"\.[^.]+$", "", item["name"].upper())
            tokens = re.split(r"[^A-Z0-9]+", name_no_ext)
            if query in tokens:
                return item
        return None

    # ── Availability / reservations ──────────────────────────────────────────
    def check_availability(self, stock_number: str) -> tuple[bool, str | None]:
        """Returns (is_available, client_remark_or_None)."""
        key = stock_number.strip().upper()
        info = self._remarks.get(key, {})
        available = info.get("available", True)   # default: in stock
        remark = info.get("client_remark") or None
        return bool(available), remark

    def set_availability(self, stock_number: str, available: bool, client_remark: str | None = None):
        key = stock_number.strip().upper()
        self._remarks[key] = {"available": available, "client_remark": client_remark}
        self._save()

    def all_remarks(self) -> dict:
        return self._remarks

    # ── Lexical fallback search ──────────────────────────────────────────────
    # Material groups — if query contains one of these, ONLY match items in the same group
    _MATERIAL_GROUPS = [
        {"pearl", "moti"},
        {"gold", "metal"},
        {"silver"},
        {"oxidized", "antique", "black"},
        {"rose-gold", "rose gold"},
        {"kundan"},
        {"meenakari"},
        {"polki"},
    ]

    # All known jewelry type words (folder names + customer query words).
    # If the query contains ANY of these, type-filtering mode is activated.
    _JEWELRY_TYPES = {
        "earrings", "earring", "necklace", "necklaces", "nath", "tikka", "tika", "maang-tikka",
        "pika", "ring", "rings", "bracelet", "bracelets", "bangle", "bangles",
        "pendant", "pendants", "set", "sets", "kaan", "chain", "chains",
        "haar", "choker", "chokar", "jhumka", "jhumki", "stud", "hoop",
        "chandbali", "bali", "kamarband", "belt", "tops",
        "hasli", "hath phool", "hathpool", "vanki",
        "gutta pussal", "kashu mala", "mango mala", "matha patti",
        "jalebi necklace", "jalebi", "magari pendant", "magari",
        "chand bali", "mini necklace",
    }

    # Maps query type words → accepted profile categories (for type exclusion).
    # Items are also matched by folder name, so this only needs to cover profile categories.
    _TYPE_CATEGORIES: dict[str, set] = {
        "earrings": {"earrings"}, "earring": {"earrings"},
        "bali": {"earrings"}, "chand bali": {"earrings"}, "chandbali": {"earrings"},
        "jhumka": {"earrings"}, "jhumki": {"earrings"},
        "tops": {"earrings"}, "stud": {"earrings"}, "hoop": {"earrings"},
        "gutta pussal": {"earrings"}, "kaan": {"earrings", "kaan"},
        "nath": {"nath"},
        "tikka": {"tikka"}, "tika": {"tikka"}, "pika": {"tikka"},
        "maang-tikka": {"tikka"}, "matha patti": {"tikka"},
        "necklace": {"necklace", "haar", "choker", "hasli"},
        "necklaces": {"necklace", "haar", "choker", "hasli"},
        "haar": {"necklace", "haar"}, "choker": {"necklace", "choker"},
        "chokar": {"necklace", "choker"},
        "hasli": {"hasli", "necklace"},
        "kashu mala": {"necklace"}, "mango mala": {"necklace", "haar"},
        "jalebi necklace": {"necklace"}, "jalebi": {"necklace"},
        "mini necklace": {"necklace"},
        "pendant": {"pendant"}, "pendants": {"pendant"},
        "magari pendant": {"pendant"}, "magari": {"pendant"},
        "bangle": {"bangle"}, "bangles": {"bangle"},
        "bracelet": {"bracelet"}, "bracelets": {"bracelet"},
        "belt": {"kamarband"}, "kamarband": {"kamarband"},
        "hath phool": {"hath phool"}, "hathpool": {"hath phool"},
        "vanki": {"vanki"},
        "ring": {"ring"}, "rings": {"ring"},
        "chain": {"chain"}, "chains": {"chain"},
        "set": {"set"}, "sets": {"set"},
    }

    def find_by_color(self, color: str, limit: int = 5) -> list[dict]:
        """Keyword-match fallback ranked by score. Folder matches are prioritised.
        The profile's category takes priority over folder name — nath never shows
        for a necklace search."""
        keywords = [k.strip().lower() for k in color.replace(",", " ").split() if k.strip()]
        if not keywords:
            return []

        query_types = {kw for kw in keywords if kw in self._JEWELRY_TYPES}

        query_material_group: set | None = None
        for group in self._MATERIAL_GROUPS:
            if any(kw in group for kw in keywords):
                query_material_group = group
                break

        scored: list[tuple[int, bool, dict]] = []
        for item in self.cache.images:
            color_tags = profile_tags(item.get("profile", {}))
            folder_tags = [f.lower() for f in item.get("folder_path", [])]
            all_tags = list(dict.fromkeys(color_tags + folder_tags))

            # Type exclusion: if query asks for a type and the item is a different
            # category, skip. Uses _TYPE_CATEGORIES to map aliases (e.g. "belt" →
            # kamarband). Falls back to folder name match so items in a "BELT"
            # folder pass even before their profile is indexed.
            if query_types:
                item_type = (item.get("profile", {}).get("category") or "").lower()
                accepted: set = set()
                for qt in query_types:
                    accepted |= self._TYPE_CATEGORIES.get(qt, {qt})
                folder_lower = {f.lower() for f in item.get("folder_path", [])}
                folder_match = bool(query_types & folder_lower) or any(
                    qt in fn or fn in qt for qt in query_types for fn in folder_lower
                )
                if item_type and item_type not in accepted and not folder_match:
                    continue

            if query_material_group:
                item_has_material = any(
                    any(tag in kw or kw in tag for tag in all_tags)
                    for kw in query_material_group
                )
                if not item_has_material:
                    continue

            non_type_keywords = [kw for kw in keywords if kw not in self._JEWELRY_TYPES]
            folder_match = any(
                any(kw in ft or ft in kw for ft in folder_tags)
                for kw in non_type_keywords
            ) if non_type_keywords else any(
                any(kw in ft or ft in kw for ft in folder_tags)
                for kw in keywords
            )

            score = sum(
                1 for kw in keywords
                if any(kw in tag or tag in kw for tag in all_tags)
            )

            if score == 0 and not folder_match:
                continue

            scored.append((score, folder_match, item))

        scored.sort(key=lambda x: (x[1], x[0]), reverse=True)
        return [item for _, _, item in scored[:limit]]
