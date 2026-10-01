"""Teaching material: notes, assessments (with question editing), PDF exports and artifact downloads."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import ContainerDep, WorkspaceDep
from app.api.schemas import (
    ArtifactOut,
    AssessmentCreated,
    AssessmentOut,
    AssessmentSummary,
    JobOut,
    NotesEdit,
    NotesOut,
    QuestionCreate,
    QuestionOut,
    QuestionPatch,
    RegenerateRequest,
)
from app.core.errors import NotFoundError
from app.db.models import Artifact, Assessment, NotesDocument
from app.jobs.queue import enqueue
from app.services.assessment.blueprint import build_blueprint
from app.services.assessment.schemas import QTYPES, AssessmentConfig
from app.services.assessment.service import suggested_question_types
from app.services.content.notes import NotesConfig
from app.services.export.service import ExportRequest

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["materials"])


# ---------------------------------------------------------------- notes


@router.post("/notes/generate", response_model=JobOut, status_code=202)
async def generate_notes(body: NotesConfig, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return await enqueue(s, kind="generate_notes", user_id=w.user_id, workspace_id=w.id,
                             payload={"config": body.model_dump()}, max_attempts=2)


@router.get("/notes", response_model=list[NotesOut])
async def list_notes(w: WorkspaceDep, c: ContainerDep, kind: str | None = None, current_only: bool = True):
    async with c.db.session() as s:
        q = select(NotesDocument).where(NotesDocument.workspace_id == w.id)
        if kind:
            q = q.where(NotesDocument.kind == kind)
        if current_only:
            q = q.where(NotesDocument.is_current)
        return (await s.execute(q.order_by(NotesDocument.created_at.desc()))).scalars().all()


@router.get("/notes/{notes_id}", response_model=NotesOut)
async def get_notes(notes_id: str, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        doc = await s.get(NotesDocument, notes_id)
    if doc is None or doc.workspace_id != w.id:
        raise NotFoundError("Notes not found")
    return doc


@router.put("/notes/{notes_id}", response_model=NotesOut)
async def edit_notes(notes_id: str, body: NotesEdit, w: WorkspaceDep, c: ContainerDep):
    """Saves the teacher's edits as a new current version (previous versions are kept)."""
    return await c.notes.save_edit(w.id, notes_id, body.title, [s.model_dump() for s in body.sections])


# ---------------------------------------------------------------- assessments


@router.get("/assessments/question-types")
async def question_types(w: WorkspaceDep):
    """Subject-appropriate defaults; every type remains selectable."""
    return {**suggested_question_types(w.workspace.topic, w.workspace.description), "all": list(QTYPES)}


@router.post("/assessments", response_model=AssessmentCreated, status_code=202)
async def create_assessment(cfg: AssessmentConfig, w: WorkspaceDep, c: ContainerDep):
    # fail fast on structural problems (counts / totals) before queueing generation
    build_blueprint("dry-run", cfg, [{"key": "x", "name": "x"}])
    a = await c.assessment.create(w.id, cfg)
    async with c.db.session() as s:
        job = await enqueue(s, kind="generate_assessment", user_id=w.user_id, workspace_id=w.id,
                            payload={"assessment_id": a.id}, idempotency_key=f"generate_assessment:{a.id}",
                            max_attempts=2)
    return AssessmentCreated(assessment=AssessmentSummary.model_validate(a), job=JobOut.model_validate(job))


@router.get("/assessments", response_model=list[AssessmentSummary])
async def list_assessments(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return (await s.execute(select(Assessment).where(Assessment.workspace_id == w.id)
                                .order_by(Assessment.created_at.desc()))).scalars().all()


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut)
async def get_assessment(assessment_id: str, w: WorkspaceDep, c: ContainerDep):
    return await c.assessment.get(w.id, assessment_id)


@router.patch("/assessments/{assessment_id}/questions/{question_id}", response_model=QuestionOut)
async def patch_question(assessment_id: str, question_id: str, body: QuestionPatch, w: WorkspaceDep,
                         c: ContainerDep):
    await c.assessment.get(w.id, assessment_id)
    return await c.assessment.update_question(w.id, question_id, body.model_dump(exclude_unset=True))


@router.delete("/assessments/{assessment_id}/questions/{question_id}", status_code=204)
async def delete_question(assessment_id: str, question_id: str, w: WorkspaceDep, c: ContainerDep):
    await c.assessment.get(w.id, assessment_id)
    await c.assessment.delete_question(w.id, question_id)


@router.post("/assessments/{assessment_id}/questions", response_model=QuestionOut, status_code=201)
async def add_question(assessment_id: str, body: QuestionCreate, w: WorkspaceDep, c: ContainerDep):
    return await c.assessment.add_manual_question(w.id, assessment_id, body.model_dump(exclude_unset=True))


@router.post("/assessments/{assessment_id}/questions/{question_id}/regenerate", response_model=JobOut,
             status_code=202)
async def regenerate_question(assessment_id: str, question_id: str, body: RegenerateRequest, w: WorkspaceDep,
                              c: ContainerDep):
    await c.assessment.get(w.id, assessment_id)
    async with c.db.session() as s:
        return await enqueue(s, kind="regenerate_question", user_id=w.user_id, workspace_id=w.id,
                             payload={"question_id": question_id, "instructions": body.instructions},
                             max_attempts=2)


# ---------------------------------------------------------------- exports / artifacts


@router.post("/exports", response_model=JobOut, status_code=202)
async def export(body: ExportRequest, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return await enqueue(s, kind="export_pdf", user_id=w.user_id, workspace_id=w.id,
                             payload={"request": body.model_dump()}, max_attempts=2)


@router.get("/artifacts", response_model=list[ArtifactOut])
async def artifacts(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return (await s.execute(select(Artifact).where(Artifact.workspace_id == w.id)
                                .order_by(Artifact.created_at.desc()))).scalars().all()


@router.get("/artifacts/{artifact_id}/download", response_class=Response,
            responses={200: {"content": {"application/pdf": {}}}})
async def download(artifact_id: str, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        art = await s.get(Artifact, artifact_id)
    if art is None or art.workspace_id != w.id:
        raise NotFoundError("Artifact not found")
    data = await c.storage.get(art.storage_path)
    safe = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in art.title)[:80].strip() or art.kind
    return Response(content=data, media_type=art.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{safe}.pdf"'})
