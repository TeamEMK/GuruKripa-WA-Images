import logging
import os
import pickle
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# v2 = OpenAI Vision profile + text-embedding-3-large vectors (3072-d).
# The old v1 cache held 2048-d ResNet/CNN vectors and is intentionally abandoned
# — a full re-index rebuilds this file. Renaming the file means the stale CNN
# cache is simply ignored rather than crashing on a dimension mismatch.
CACHE_FILE = "cache/embeddings_v2.pkl"
IMAGES_DIR = "cache/images"
MATCHES_FILE = "cache/matches.pkl"
MAX_MATCH_HISTORY = 100


def _normalize(vec: np.ndarray) -> np.ndarray:
    return vec / (np.linalg.norm(vec) + 1e-8)


def _atomic_pickle(path: str, obj) -> None:
    """Write then atomically rename — a crash mid-write can't corrupt the file."""
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        pickle.dump(obj, f)
    os.replace(tmp, path)


class CacheService:
    def __init__(self):
        os.makedirs(IMAGES_DIR, exist_ok=True)
        self.images: list[dict] = []
        self.last_updated: Optional[datetime] = None
        self.match_history: list[dict] = []
        # Subset of self.images that have an embedding, plus its stacked matrix.
        self._emb_items: list[dict] = []
        self._emb_matrix: Optional[np.ndarray] = None
        self._load()

    def _load(self):
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "rb") as f:
                saved = pickle.load(f)
            self.images = saved.get("images", [])
            self.last_updated = saved.get("last_updated")
            self._rebuild_matrix()
            logger.info(f"Loaded {len(self.images)} cached image profiles "
                        f"({len(self._emb_items)} with embeddings)")

        if os.path.exists(MATCHES_FILE):
            with open(MATCHES_FILE, "rb") as f:
                self.match_history = pickle.load(f)

    def _rebuild_matrix(self):
        self._emb_items = [d for d in self.images if d.get("embedding") is not None]
        if self._emb_items:
            self._emb_matrix = np.stack([d["embedding"] for d in self._emb_items])
        else:
            self._emb_matrix = np.array([])

    def upsert(
        self,
        drive_id: str,
        name: str,
        local_path: str,
        folder_path: list[str] | None = None,
        profile: dict | None = None,
        embedding: np.ndarray | None = None,
    ):
        self.images = [d for d in self.images if d["id"] != drive_id]
        self.images.append({
            "id": drive_id,
            "name": name,
            "local_path": local_path,
            "folder_path": folder_path or [],
            "profile": profile or {},
            "embedding": _normalize(embedding) if embedding is not None else None,
        })

    def finalize(self):
        self.last_updated = datetime.now()
        self._rebuild_matrix()
        _atomic_pickle(CACHE_FILE, {"images": self.images, "last_updated": self.last_updated})
        logger.info(f"Cache saved: {len(self.images)} images, {len(self._emb_items)} embedded")

    def find_semantic(
        self, query_vec: list[float] | np.ndarray, k: int = 5, min_score: float = 0.0
    ) -> list[tuple[dict, float]]:
        """Cosine-similarity search over the OpenAI embedding vectors.
        Returns up to k (image_item, score) pairs, best first, dropping anything
        below min_score. Unified engine for image→image and text→image search."""
        if self._emb_matrix is None:
            self._rebuild_matrix()
        if self._emb_matrix.size == 0 or query_vec is None or len(query_vec) == 0:
            return []
        q = _normalize(np.asarray(query_vec, dtype=np.float32))
        if q.shape[0] != self._emb_matrix.shape[1]:
            logger.error(f"Query dim {q.shape[0]} != index dim {self._emb_matrix.shape[1]} — re-index needed")
            return []
        sims = self._emb_matrix @ q  # both sides normalized → dot = cosine
        top = min(k, len(sims))
        idx = np.argpartition(sims, -top)[-top:]
        idx = idx[np.argsort(sims[idx])[::-1]]
        return [(self._emb_items[i], float(sims[i])) for i in idx if sims[i] >= min_score]

    def log_match(self, sender: str, query_url: str, matches: list[dict], scores: list[float]):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "sender": sender,
            "query_url": query_url,
            "matches": [{"name": m["name"], "score": round(s * 100, 1)} for m, s in zip(matches, scores)],
        }
        self.match_history.insert(0, entry)
        self.match_history = self.match_history[:MAX_MATCH_HISTORY]
        _atomic_pickle(MATCHES_FILE, self.match_history)

    def profile_stats(self) -> dict:
        total = len(self.images)
        profiled = len(self._emb_items)
        return {"total_images": total, "profiled": profiled, "pending": total - profiled}

    def stats(self) -> dict:
        size = sum(
            os.path.getsize(d["local_path"])
            for d in self.images
            if os.path.exists(d["local_path"])
        )
        return {
            "total_images": len(self.images),
            "profiled": len(self._emb_items),
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "cache_size_mb": round(size / 1024 / 1024, 2),
        }
