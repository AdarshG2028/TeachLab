"""Semantic retrieval over indexed source chunks, hydrated from PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import Chunk, Source

if TYPE_CHECKING:
    from app.container import Container


@dataclass
class RetrievedPassage:
    chunk_id: str
    source_id: str
    title: str
    url: str | None
    text: str
    score: float
    published_at: str | None = None


class RetrievalService:
    def __init__(self, c: Container) -> None:
        self.c = c

    async def embed_query(self, query: str) -> list[float]:
        return await asyncio.to_thread(self.c.embeddings.embed_query, query)

    async def search(self, workspace_id: str, query: str, *, top_k: int | None = None,
                     source_ids: list[str] | None = None, min_score: float | None = None) -> list[RetrievedPassage]:
        top_k = top_k or self.c.settings.retrieval_top_k
        vector = await self.embed_query(query)
        hits = await self.c.vectors.search(workspace_id, vector, top_k=top_k, source_ids=source_ids)
        if min_score is not None:
            hits = [h for h in hits if h.score >= min_score]
        return await self.hydrate(workspace_id, [(h.id, h.score) for h in hits])

    async def hydrate(self, workspace_id: str, scored: list[tuple[str, float]]) -> list[RetrievedPassage]:
        """Load chunk text from PostgreSQL, re-checking workspace ownership of every chunk."""
        if not scored:
            return []
        ids = [cid for cid, _ in scored]
        async with self.c.db.session() as s:
            rows = (await s.execute(
                select(Chunk, Source.title, Source.url, Source.published_at)
                .join(Source, Source.id == Chunk.source_id)
                .where(Chunk.id.in_(ids), Chunk.workspace_id == workspace_id))).all()
        by_id = {r[0].id: r for r in rows}
        out = []
        for cid, score in scored:
            r = by_id.get(cid)
            if r is None:
                continue  # stale vector (source deleted) or foreign id: never returned
            chunk, title, url, published = r
            out.append(RetrievedPassage(chunk_id=chunk.id, source_id=chunk.source_id, title=title or "", url=url,
                                        text=chunk.text, score=score, published_at=published))
        return out

    async def chunks_by_ids(self, workspace_id: str, chunk_ids: list[str]) -> list[RetrievedPassage]:
        return await self.hydrate(workspace_id, [(cid, 1.0) for cid in chunk_ids])
