"""Lightweight local embeddings via fastembed (ONNX Runtime, no PyTorch).

Produces the same vectors as the sentence-transformers implementation of BAAI/bge-small-en-v1.5 (cosine 1.0
in our comparison), so existing Qdrant indexes stay valid. Measured whole-process memory with the model
loaded: about 200 MB, versus about 520 MB with PyTorch, and no multi-GB torch install.

Memory notes: onnxruntime's CPU memory arena is disabled (it otherwise reserves hundreds of MB) and the
thread count is capped.
"""

from __future__ import annotations

import threading
from functools import lru_cache

from app.core.config import Settings
from app.core.logging import get_logger
from app.providers.embeddings.base import EmbeddingProvider

log = get_logger(__name__)


class FastEmbedProvider(EmbeddingProvider):
    def __init__(self, model_name: str, *, threads: int = 2, batch_size: int = 32, query_instruction: str = "",
                 cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding

        self.model_name = model_name
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        log.info("embedding_model_loading", backend="fastembed", model=model_name, threads=threads)
        self._model = TextEmbedding(model_name, cache_dir=cache_dir, threads=threads, enable_cpu_mem_arena=False)
        self.dim = int(next(iter(self._model.embed(["dimension probe"]))).shape[0])
        self._lock = threading.Lock()
        log.info("embedding_model_loaded", backend="fastembed", model=model_name, dim=self.dim)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            # fastembed returns L2-normalized vectors for BGE models
            return [v.tolist() for v in self._model.embed(texts, batch_size=self.batch_size)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode([t.replace("\n", " ").strip() for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self.query_instruction + text.strip()])[0]


@lru_cache(maxsize=2)
def _cached(model_name: str, threads: int, batch_size: int, instruction: str, cache_dir: str | None):
    return FastEmbedProvider(model_name, threads=threads, batch_size=batch_size, query_instruction=instruction,
                             cache_dir=cache_dir)


def get_fastembed(settings: Settings) -> FastEmbedProvider:
    """Process-wide singleton per configuration."""
    return _cached(settings.embedding_model, settings.embedding_threads, settings.embedding_batch_size,
                   settings.embedding_query_instruction,
                   str(settings.embedding_cache_dir) if settings.embedding_cache_dir else None)
