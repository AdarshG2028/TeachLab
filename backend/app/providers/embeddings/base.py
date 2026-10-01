"""Embedding provider interface (synchronous; CPU-bound callers should use a thread)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    model_name: str
    dim: int

    @property
    def version(self) -> str:
        """Identifier stored with every vector so indexes can be rebuilt on model change."""
        return f"{self.model_name}@{self.dim}"

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...
