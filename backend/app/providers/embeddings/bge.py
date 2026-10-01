"""Local BGE embeddings via sentence-transformers.

The model is loaded once per process (``get_bge``), runs on CPU by default,
returns L2-normalized vectors (cosine == dot product), and applies the BGE
query instruction only to queries, as recommended for bge-*-en-v1.5.

Resource notes (bge-small-en-v1.5, CPU): ~130 MB weights; process RSS grows by
~0.8-1.2 GB because of torch. Throughput on 4 CPU cores is roughly 50-150
chunks/s at 350 tokens. Only the worker process loads it.
"""

from __future__ import annotations

import threading
from functools import lru_cache

from app.core.config import Settings
from app.core.logging import get_logger
from app.providers.embeddings.base import EmbeddingProvider

log = get_logger(__name__)


class BGEEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_name: str, *, device: str = "cpu", batch_size: int = 32,
                 query_instruction: str = "", cache_dir: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        log.info("embedding_model_loading", model=model_name, device=device)
        self._model = SentenceTransformer(model_name, device=device, cache_folder=cache_dir)
        get_dim = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        self.dim = int(get_dim())
        self._lock = threading.Lock()  # sentence-transformers encode is not guaranteed thread-safe
        log.info("embedding_model_loaded", model=model_name, dim=self.dim)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            vectors = self._model.encode(
                texts, batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False,
                convert_to_numpy=True,
            )
        return [v.tolist() for v in vectors]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode([t.replace("\n", " ").strip() for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self.query_instruction + text.strip()])[0]


@lru_cache(maxsize=2)
def _cached(model_name: str, device: str, batch_size: int, instruction: str, cache_dir: str | None) -> BGEEmbeddingProvider:
    return BGEEmbeddingProvider(model_name, device=device, batch_size=batch_size, query_instruction=instruction,
                                cache_dir=cache_dir)


def get_bge(settings: Settings) -> BGEEmbeddingProvider:
    """Process-wide singleton per model configuration."""
    return _cached(settings.embedding_model, settings.embedding_device, settings.embedding_batch_size,
                   settings.embedding_query_instruction,
                   str(settings.embedding_cache_dir) if settings.embedding_cache_dir else None)
