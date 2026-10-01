"""Source ingestion: dedupe -> normalize -> store text -> chunk -> embed -> index.

PostgreSQL holds the normalized text and chunks (durable archive); Qdrant only
holds vectors plus identifiers that point back to them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ValidationFailed
from app.core.ids import canonical_url, sha256_text, url_hash
from app.core.logging import get_logger
from app.db.models import Chunk, Source
from app.providers.search.base import SearchHit
from app.providers.vector.base import VectorPoint
from app.services.research.chunker import chunk_text
from app.services.research.documents import NormalizedDocument, extract_document, normalize_text
from app.services.research.fetcher import SafeFetcher

if TYPE_CHECKING:
    from app.container import Container

log = get_logger(__name__)

MIN_TEXT_CHARS = 200


@dataclass
class IngestResult:
    source_id: str
    created: bool
    reason: str = ""


class IngestionService:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.settings = c.settings
        self.fetcher = SafeFetcher(max_bytes=self.settings.fetch_max_mb * 1024 * 1024,
                                   timeout=self.settings.fetch_timeout_seconds)

    async def _existing(self, workspace_id: str, uhash: str, content_hash: str | None) -> str | None:
        async with self.c.db.session() as s:
            q = select(Source.id).where(Source.workspace_id == workspace_id, Source.url_hash == uhash)
            sid = (await s.execute(q)).scalar_one_or_none()
            if sid is None and content_hash:
                sid = (await s.execute(select(Source.id).where(
                    Source.workspace_id == workspace_id, Source.content_hash == content_hash).limit(1))
                       ).scalar_one_or_none()
        return sid

    async def _store(self, workspace_id: str, *, origin: str, url: str | None, uhash: str, doc: NormalizedDocument,
                     storage_path: str | None = None, meta: dict | None = None) -> IngestResult:
        text = doc.text
        if len(text) < MIN_TEXT_CHARS:
            raise ValidationFailed("Document has too little extractable text")
        content_hash = sha256_text(text)
        existing = await self._existing(workspace_id, uhash, content_hash)
        if existing:
            return IngestResult(existing, False, "duplicate")
        async with self.c.db.session() as s:
            stmt = pg_insert(Source).values(
                workspace_id=workspace_id, origin=origin, url=url, canonical_url=canonical_url(url) if url else None,
                url_hash=uhash, title=(doc.title or url or "Untitled")[:1000], site_name=doc.site_name[:300],
                published_at=(doc.published_at or None), content_type=doc.content_type, content_hash=content_hash,
                text=text, storage_path=storage_path, status="ingested", meta=meta or {},
            ).on_conflict_do_nothing(index_elements=["workspace_id", "url_hash"]).returning(Source.id)
            sid = (await s.execute(stmt)).scalar_one_or_none()
            if sid is None:  # concurrent insert won the race
                sid = (await s.execute(select(Source.id).where(Source.workspace_id == workspace_id,
                                                               Source.url_hash == uhash))).scalar_one()
                return IngestResult(sid, False, "duplicate")
            chunks = chunk_text(sid, text, target_tokens=self.settings.chunk_target_tokens,
                                overlap_tokens=self.settings.chunk_overlap_tokens)
            if chunks:
                await s.execute(pg_insert(Chunk).values([
                    {"id": ch.id, "source_id": sid, "workspace_id": workspace_id, "idx": ch.idx, "text": ch.text,
                     "start_char": ch.start_char, "end_char": ch.end_char, "token_count": ch.token_count,
                     "heading": ch.heading[:500]} for ch in chunks
                ]).on_conflict_do_nothing())
        log.info("source_ingested", source_id=sid, origin=origin, chunks=len(chunks))
        return IngestResult(sid, True)

    async def ingest_search_hit(self, workspace_id: str, hit: SearchHit, query: str) -> IngestResult:
        uhash = url_hash(hit.url)
        existing = await self._existing(workspace_id, uhash, None)
        if existing:
            return IngestResult(existing, False, "duplicate")
        content = hit.content
        if len(content) < MIN_TEXT_CHARS:
            pages = await self.c.search.extract([hit.url])
            content = pages[0].content if pages else ""
        if len(content) < MIN_TEXT_CHARS:
            return IngestResult("", False, "no_content")
        doc = NormalizedDocument(title=hit.title, text=normalize_text(content), content_type="text/html",
                                 published_at=hit.published_date)
        try:
            return await self._store(workspace_id, origin="search", url=hit.url, uhash=uhash, doc=doc,
                                     meta={"query": query, "search_score": hit.score, "provider": self.c.search.name})
        except ValidationFailed as exc:
            return IngestResult("", False, exc.message)

    async def ingest_url(self, workspace_id: str, url: str) -> IngestResult:
        uhash = url_hash(url)
        existing = await self._existing(workspace_id, uhash, None)
        if existing:
            return IngestResult(existing, False, "duplicate")
        fetched = await self.fetcher.fetch(url)
        doc = await asyncio.to_thread(extract_document, fetched.body, fetched.content_type, fetched.final_url)
        storage_path = None
        if fetched.content_type == "application/pdf":
            storage_path = f"{workspace_id}/raw/{uhash}.pdf"
            await self.c.storage.put(storage_path, fetched.body, "application/pdf")
        return await self._store(workspace_id, origin="url", url=url, uhash=uhash, doc=doc, storage_path=storage_path,
                                 meta={"final_url": fetched.final_url})

    async def ingest_upload(self, workspace_id: str, filename: str, storage_key: str, content_type: str) -> IngestResult:
        data = await self.c.storage.get(storage_key)
        doc = await asyncio.to_thread(extract_document, data, content_type, None)
        if not doc.title:
            doc.title = filename
        uhash = sha256_text("upload:" + sha256_text(doc.text))
        return await self._store(workspace_id, origin="upload", url=None, uhash=uhash, doc=doc,
                                 storage_path=storage_key, meta={"filename": filename})

    async def index_source(self, workspace_id: str, source_id: str) -> int:
        """Embed and upsert all chunks of a source. Idempotent: point IDs are the stable chunk IDs."""
        async with self.c.db.session() as s:
            src = await s.get(Source, source_id)
            if src is None or src.workspace_id != workspace_id:
                raise ValidationFailed("Source not in workspace")
            chunks = (await s.execute(select(Chunk).where(Chunk.source_id == source_id).order_by(Chunk.idx))
                      ).scalars().all()
            title, url = src.title, src.url
        if not chunks:
            return 0
        emb = self.c.embeddings
        texts = [(f"{c.heading}\n" if c.heading else "") + c.text for c in chunks]
        vectors = await asyncio.to_thread(emb.embed_documents, texts)
        points = [VectorPoint(id=c.id, vector=v, payload={
            "workspace_id": workspace_id, "source_id": source_id, "chunk_id": c.id, "kind": "source_chunk",
            "idx": c.idx, "title": title[:300], "url": url, "embedding_model": emb.version,
        }) for c, v in zip(chunks, vectors, strict=True)]
        await self.c.vectors.upsert(workspace_id, points)
        async with self.c.db.session() as s:
            await s.execute(update(Source).where(Source.id == source_id).values(
                indexed_at=datetime.now(UTC), embedding_model=emb.version,
                status="indexed" if src.status == "ingested" else src.status))
        return len(points)

    async def reindex_workspace(self, workspace_id: str) -> int:
        async with self.c.db.session() as s:
            ids = (await s.execute(select(Source.id).where(Source.workspace_id == workspace_id))).scalars().all()
        total = 0
        for sid in ids:
            total += await self.index_source(workspace_id, sid)
        return total
