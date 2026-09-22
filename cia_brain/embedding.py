from __future__ import annotations

import httpx
import numpy as np

from .settings import Settings


class Embedder:
    def __init__(self, settings: Settings):
        self.s = settings
        self.backend = settings.embedding_backend.lower()
        self._model = None
        self._dim = None
        if self.backend == "fastembed":
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=settings.embedding_model, cache_dir=str(settings.data_dir / "models"))
        elif self.backend != "ollama":
            raise ValueError(f"unsupported EMBEDDING_BACKEND={self.backend}")

    @property
    def model_name(self) -> str:
        return self.s.embedding_model if self.backend == "fastembed" else self.s.ollama_embed_model

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        if self.backend == "fastembed":
            arr = np.asarray(list(self._model.embed(texts)), dtype=np.float32)
        else:
            with httpx.Client(timeout=300) as client:
                resp = client.post(
                    f"{self.s.ollama_url}/api/embed",
                    json={"model": self.s.ollama_embed_model, "input": texts, "truncate": True},
                )
                resp.raise_for_status()
                arr = np.asarray(resp.json()["embeddings"], dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        self._dim = int(arr.shape[1])
        return arr

    @property
    def dimension(self) -> int:
        if self._dim is None:
            self._dim = int(self.embed(["dimension probe"])[0].shape[0])
        return self._dim
