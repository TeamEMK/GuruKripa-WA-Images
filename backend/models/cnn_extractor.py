import io
import logging
import ssl

import numpy as np

# macOS Python 3.12 ships without system CA certs — needed for the one-time model weight download
ssl._create_default_https_context = ssl._create_unverified_context
import torch
import torch.nn as nn
import torchvision.models as models
import torchvision.transforms as transforms
from PIL import Image

logger = logging.getLogger(__name__)


class CNNExtractor:
    def __init__(self):
        logger.info("Loading ResNet50 model (pretrained on ImageNet)...")
        base = models.resnet50(weights=models.ResNet50_Weights.IMAGENET1K_V1)
        # Remove the final FC classification layer — use avg-pooled 2048-dim features
        self.model = nn.Sequential(*list(base.children())[:-1])
        self.model.eval()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = self.model.to(self.device)
        logger.info(f"Model ready on {self.device}")

        self.transform = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])

    def extract(self, image_bytes: bytes) -> np.ndarray:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        tensor = self.transform(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            features = self.model(tensor)
        vec = features.squeeze().cpu().numpy()  # (2048,)
        return vec / (np.linalg.norm(vec) + 1e-8)  # L2-normalize

    def find_top_k(
        self, query: np.ndarray, embedding_matrix: np.ndarray, k: int = 5
    ) -> list[tuple[int, float]]:
        if embedding_matrix.size == 0:
            return []
        # Both query and stored embeddings are already L2-normalized → dot product = cosine sim
        similarities = embedding_matrix @ query  # (N,)
        top_k = min(k, len(similarities))
        top_indices = np.argpartition(similarities, -top_k)[-top_k:]
        top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]
        return [(int(idx), float(similarities[idx])) for idx in top_indices]
