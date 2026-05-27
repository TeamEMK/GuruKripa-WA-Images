import logging
import os
import pickle
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

CACHE_FILE = "cache/embeddings.pkl"
IMAGES_DIR = "cache/images"
MATCHES_FILE = "cache/matches.pkl"
MAX_MATCH_HISTORY = 100


class CacheService:
    def __init__(self):
        os.makedirs(IMAGES_DIR, exist_ok=True)
        self.images: list[dict] = []
        self.last_updated: Optional[datetime] = None
        self._embedding_matrix: Optional[np.ndarray] = None
        self.match_history: list[dict] = []
        self._load()

    def _load(self):
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "rb") as f:
                saved = pickle.load(f)
            self.images = saved.get("images", [])
            self.last_updated = saved.get("last_updated")
            self._rebuild_matrix()
            logger.info(f"Loaded {len(self.images)} cached embeddings")

        if os.path.exists(MATCHES_FILE):
            with open(MATCHES_FILE, "rb") as f:
                self.match_history = pickle.load(f)

    def _rebuild_matrix(self):
        if self.images:
            self._embedding_matrix = np.stack([d["embedding"] for d in self.images])
        else:
            self._embedding_matrix = np.array([])

    def get_embedding_matrix(self) -> np.ndarray:
        if self._embedding_matrix is None:
            self._rebuild_matrix()
        return self._embedding_matrix

    def upsert(self, drive_id: str, name: str, local_path: str, embedding: np.ndarray, folder_path: list[str] | None = None):
        self.images = [d for d in self.images if d["id"] != drive_id]
        self.images.append({
            "id": drive_id,
            "name": name,
            "local_path": local_path,
            "embedding": embedding,
            "folder_path": folder_path or [],
        })

    def finalize(self):
        self.last_updated = datetime.now()
        self._rebuild_matrix()
        with open(CACHE_FILE, "wb") as f:
            pickle.dump({"images": self.images, "last_updated": self.last_updated}, f)
        logger.info(f"Cache saved: {len(self.images)} images")

    def get_by_indices(self, indices: list[int]) -> list[dict]:
        return [self.images[i] for i in indices]

    def log_match(self, sender: str, query_url: str, matches: list[dict], scores: list[float]):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "sender": sender,
            "query_url": query_url,
            "matches": [{"name": m["name"], "score": round(s * 100, 1)} for m, s in zip(matches, scores)],
        }
        self.match_history.insert(0, entry)
        self.match_history = self.match_history[:MAX_MATCH_HISTORY]
        with open(MATCHES_FILE, "wb") as f:
            pickle.dump(self.match_history, f)

    def stats(self) -> dict:
        size = sum(
            os.path.getsize(d["local_path"])
            for d in self.images
            if os.path.exists(d["local_path"])
        )
        return {
            "total_images": len(self.images),
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "cache_size_mb": round(size / 1024 / 1024, 2),
        }
