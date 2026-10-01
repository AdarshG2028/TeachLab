"""Qdrant vector store (server, Qdrant Cloud, or embedded local mode).

The collection name encodes the embedding model and dimension, so switching
models creates a new collection; ``reindex-vectors`` repopulates it from
PostgreSQL chunks. Every query is filtered by ``workspace_id``.
"""

from __future__ import annotations

import re

from qdrant_client import AsyncQdrantClient, models

from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.providers.vector.base import VectorHit, VectorPoint, VectorStore

log = get_logger(__name__)


def collection_name(prefix: str, model_name: str, dim: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-")
    return f"{prefix}__{slug}__{dim}"


class QdrantVectorStore(VectorStore):
    def __init__(self, client: AsyncQdrantClient, collection: str, dim: int) -> None:
        self.client = client
        self.collection = collection
        self.dim = dim
        self._ready = False

    @classmethod
    def from_settings(cls, *, url: str | None, api_key: str | None, path: str, collection: str,
                      dim: int) -> "QdrantVectorStore":
        if url:
            client = AsyncQdrantClient(url=url, api_key=api_key, timeout=30)
        elif path == ":memory:":
            client = AsyncQdrantClient(location=":memory:")
        else:
            client = AsyncQdrantClient(path=path)
        return cls(client, collection, dim)

    async def ensure_collection(self) -> None:
        if self._ready:
            return
        try:
            if not await self.client.collection_exists(self.collection):
                await self.client.create_collection(
                    self.collection,
                    vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE),
                )
                log.info("qdrant_collection_created", collection=self.collection, dim=self.dim)
            for field in ("workspace_id", "source_id", "kind"):
                try:
                    await self.client.create_payload_index(
                        self.collection, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
                    )
                except Exception:  # index exists, or local mode (indexes unsupported/no-op)
                    pass
        except Exception as exc:
            raise ProviderError(f"Qdrant unavailable: {exc}", retryable=True) from exc
        self._ready = True

    @staticmethod
    def _filter(workspace_id: str, source_ids: list[str] | None = None, kind: str | None = None) -> models.Filter:
        must: list[models.Condition] = [
            models.FieldCondition(key="workspace_id", match=models.MatchValue(value=workspace_id))
        ]
        if source_ids:
            must.append(models.FieldCondition(key="source_id", match=models.MatchAny(any=source_ids)))
        if kind:
            must.append(models.FieldCondition(key="kind", match=models.MatchValue(value=kind)))
        return models.Filter(must=must)

    async def upsert(self, workspace_id: str, points: list[VectorPoint]) -> None:
        if not points:
            return
        await self.ensure_collection()
        for p in points:
            if p.payload.get("workspace_id") != workspace_id:
                raise ValueError("Vector payload workspace_id mismatch")
            if len(p.vector) != self.dim:
                raise ValueError(f"Vector dim {len(p.vector)} != collection dim {self.dim}")
        try:
            for i in range(0, len(points), 128):
                batch = points[i : i + 128]
                await self.client.upsert(
                    self.collection,
                    points=[models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in batch],
                    wait=True,
                )
        except Exception as exc:
            raise ProviderError(f"Qdrant upsert failed: {exc}", retryable=True) from exc

    async def search(self, workspace_id: str, vector: list[float], *, top_k: int = 8,
                     source_ids: list[str] | None = None, kind: str | None = None) -> list[VectorHit]:
        await self.ensure_collection()
        try:
            res = await self.client.query_points(
                self.collection, query=vector, limit=top_k, with_payload=True,
                query_filter=self._filter(workspace_id, source_ids, kind),
            )
        except Exception as exc:
            raise ProviderError(f"Qdrant search failed: {exc}", retryable=True) from exc
        return [VectorHit(id=str(p.id), score=float(p.score), payload=dict(p.payload or {})) for p in res.points]

    async def delete_source(self, workspace_id: str, source_id: str) -> None:
        await self.ensure_collection()
        await self.client.delete(self.collection, points_selector=models.FilterSelector(
            filter=self._filter(workspace_id, [source_id])))

    async def delete_workspace(self, workspace_id: str) -> None:
        await self.ensure_collection()
        await self.client.delete(self.collection, points_selector=models.FilterSelector(
            filter=self._filter(workspace_id)))

    async def count(self, workspace_id: str) -> int:
        await self.ensure_collection()
        res = await self.client.count(self.collection, count_filter=self._filter(workspace_id), exact=True)
        return int(res.count)

    async def close(self) -> None:
        await self.client.close()
