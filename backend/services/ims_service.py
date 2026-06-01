import json
import logging
import os
import re

logger = logging.getLogger(__name__)

REMARKS_FILE = "cache/ims_remarks.json"
COLOR_INDEX_FILE = "cache/color_index.json"


class IMSService:
    def __init__(self, cache_service):
        self.cache = cache_service
        self._remarks: dict = {}
        self._color_index: dict = {}  # filename → ["gold", "white diamond"]
        self._load()

    def _load(self):
        if os.path.exists(REMARKS_FILE):
            with open(REMARKS_FILE) as f:
                self._remarks = json.load(f)
            logger.info(f"Loaded {len(self._remarks)} IMS remarks")
        if os.path.exists(COLOR_INDEX_FILE):
            with open(COLOR_INDEX_FILE) as f:
                self._color_index = json.load(f)
            logger.info(f"Loaded color index: {len(self._color_index)} entries")

    def _save(self):
        os.makedirs("cache", exist_ok=True)
        with open(REMARKS_FILE, "w") as f:
            json.dump(self._remarks, f, indent=2)

    def _save_colors(self):
        os.makedirs("cache", exist_ok=True)
        with open(COLOR_INDEX_FILE, "w") as f:
            json.dump(self._color_index, f, indent=2)

    def find_by_stock_number(self, stock_number: str) -> dict | None:
        """Search cached Drive images for a filename that matches the stock number."""
        query = stock_number.strip().upper()
        for item in self.cache.images:
            # item["name"] is the original Drive filename, e.g. "9858.JPG" or "S-1655.JPG"
            name_no_ext = re.sub(r"\.[^.]+$", "", item["name"].upper())
            if query == name_no_ext:
                return item
        # Second pass: partial match (e.g. "Copy of 9858" → "9858")
        for item in self.cache.images:
            name_no_ext = re.sub(r"\.[^.]+$", "", item["name"].upper())
            if query in name_no_ext:
                return item
        return None

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

    def find_by_color(self, color: str, limit: int = 10) -> list[dict]:
        """Return items ranked by keyword match score. Folder matches are prioritised.
        Material-exclusive: pearl query only returns pearl items, gold only gold, etc."""
        keywords = [k.strip().lower() for k in color.replace(",", " ").split() if k.strip()]
        if not keywords:
            return []

        query_material_group: set | None = None
        for group in self._MATERIAL_GROUPS:
            if any(kw in group for kw in keywords):
                query_material_group = group
                break

        scored: list[tuple[int, bool, dict]] = []
        for item in self.cache.images:
            color_tags = [t.lower() for t in self._color_index.get(item["name"], [])]
            folder_tags = [f.lower() for f in item.get("folder_path", [])]
            all_tags = list(dict.fromkeys(color_tags + folder_tags))

            if query_material_group:
                item_has_material = any(
                    any(tag in kw or kw in tag for tag in all_tags)
                    for kw in query_material_group
                )
                if not item_has_material:
                    continue

            # Folder match — item is in a folder whose name matches a keyword
            folder_match = any(
                any(kw in ft or ft in kw for ft in folder_tags)
                for kw in keywords
            )

            score = sum(
                1 for kw in keywords
                if any(kw in tag or tag in kw for tag in all_tags)
            )

            # Include if folder matches even when GPT missed some tags
            if score == 0 and not folder_match:
                continue

            scored.append((score, folder_match, item))

        # Folder matches first, then by keyword score descending
        scored.sort(key=lambda x: (x[1], x[0]), reverse=True)
        return [item for _, _, item in scored[:limit]]

    def update_color_index(self, filename: str, colors: list[str]):
        self._color_index[filename] = colors
        self._save_colors()

    def all_remarks(self) -> dict:
        return self._remarks

    def color_index_stats(self) -> dict:
        total = len(self.cache.images)
        indexed = sum(1 for item in self.cache.images if item["name"] in self._color_index)
        return {"total_images": total, "color_indexed": indexed, "pending": total - indexed}
