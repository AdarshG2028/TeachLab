"""Explicit research requests and source management (search, URLs, uploads)."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import ContainerDep, WorkspaceDep
from app.api.schemas import ChunkOut, JobOut, ResearchRequest, SourceDetail, SourceOut, UrlIngestRequest
from app.core.errors import NotFoundError
from app.core.ids import sha256_text
from app.db.models import Source
from app.jobs.queue import enqueue
from app.services.research.documents import validate_upload
from app.services.research.fetcher import resolve_public

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["research"])


@router.post("/research", response_model=JobOut, status_code=202)
async def start_research(body: ResearchRequest, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return await enqueue(s, kind="research", user_id=w.user_id, workspace_id=w.id,
                             payload=body.model_dump(), max_attempts=3)


@router.post("/sources/url", response_model=JobOut, status_code=202)
async def add_url(body: UrlIngestRequest, w: WorkspaceDep, c: ContainerDep):
    url = str(body.url)
    await resolve_public(url)  # reject private/loopback targets before queueing (re-checked at fetch time)
    async with c.db.session() as s:
        return await enqueue(s, kind="ingest_url", user_id=w.user_id, workspace_id=w.id,
                             payload={"url": url, "session_id": body.session_id},
                             idempotency_key=f"ingest_url:{w.id}:{sha256_text(url)[:40]}", max_attempts=3)


@router.post("/sources/upload", response_model=JobOut, status_code=202)
async def upload(w: WorkspaceDep, c: ContainerDep, file: UploadFile = File(...),
                 session_id: str | None = Form(default=None)):
    max_bytes = c.settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    filename = (file.filename or "upload").replace("\\", "/").split("/")[-1][:200]
    ctype = validate_upload(filename, data, max_bytes)
    digest = sha256_text(data.hex())
    ext = filename.rsplit(".", 1)[-1].lower()
    key = f"{w.id}/uploads/{digest[:32]}.{ext}"
    await c.storage.put(key, data, ctype)
    async with c.db.session() as s:
        return await enqueue(s, kind="ingest_upload", user_id=w.user_id, workspace_id=w.id,
                             payload={"storage_key": key, "filename": filename, "content_type": ctype,
                                      "session_id": session_id},
                             idempotency_key=f"ingest_upload:{w.id}:{digest[:40]}", max_attempts=2)


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return (await s.execute(select(Source).where(Source.workspace_id == w.id)
                                .order_by(Source.retrieved_at.desc()))).scalars().all()


@router.get("/sources/{source_id}", response_model=SourceDetail)
async def get_source(source_id: str, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        src = (await s.execute(select(Source).where(Source.id == source_id, Source.workspace_id == w.id)
                               .options(selectinload(Source.chunks)))).scalar_one_or_none()
    if src is None:
        raise NotFoundError("Source not found")
    base = SourceOut.model_validate(src).model_dump()
    return SourceDetail(**base, text_preview=src.text[:4000],
                        chunks=[ChunkOut.model_validate(ch) for ch in src.chunks[:200]])
