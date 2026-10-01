"""Roadmap (versioned teaching plan), phase navigation, coverage and gap detection."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import ContainerDep, WorkspaceDep
from app.api.routers.workspaces import post_chat_message
from app.api.schemas import (
    ChatTurnOut,
    CoverageMark,
    CoverageOut,
    JobOut,
    NavigateRequest,
    PhaseStatusUpdate,
    RoadmapEdit,
    RoadmapGenerate,
    RoadmapItemOut,
    RoadmapOut,
    RoadmapPhaseOut,
    RoadmapVersionOut,
    StartPhaseRequest,
)
from app.core.errors import NotFoundError
from app.db.models import RoadmapVersion
from app.jobs.queue import enqueue

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["planning"])


def _coverage_dict(rec) -> dict:
    if rec is None:
        return {"planned": False, "researched": False, "explained": False, "discussed": False, "completed": False}
    return {"planned": bool(rec.planned_at), "researched": bool(rec.researched_at),
            "explained": bool(rec.explained_at), "discussed": bool(rec.discussed_at),
            "completed": bool(rec.completed_at), "completed_origin": rec.completed_origin,
            "self_reported_understanding": rec.self_reported_understanding}


@router.get("/roadmap", response_model=RoadmapOut | None)
async def get_roadmap(w: WorkspaceDep, c: ContainerDep):
    rm = await c.roadmap.current(w.id)
    if rm is None:
        return None
    recs = await c.coverage.records(w.id)
    phases = []
    for ph in rm.phases:
        items = [RoadmapItemOut.model_validate(it).model_copy(update={"coverage": _coverage_dict(
            recs.get(it.concept_key))}) for it in ph.items]
        phases.append(RoadmapPhaseOut.model_validate(ph).model_copy(update={"items": items}))
    return RoadmapOut.model_validate(rm).model_copy(update={"phases": phases})


@router.get("/roadmap/versions", response_model=list[RoadmapVersionOut])
async def versions(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return (await s.execute(select(RoadmapVersion).where(RoadmapVersion.workspace_id == w.id)
                                .order_by(RoadmapVersion.version.desc()))).scalars().all()


@router.post("/roadmap/generate", response_model=JobOut, status_code=202)
async def generate(body: RoadmapGenerate, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return await enqueue(s, kind="generate_roadmap", user_id=w.user_id, workspace_id=w.id,
                             payload={"instructions": body.instructions}, max_attempts=2)


@router.put("/roadmap", response_model=RoadmapOut)
async def edit_roadmap(body: RoadmapEdit, w: WorkspaceDep, c: ContainerDep):
    """Save teacher edits (reorder, include/exclude, edit, add) as a new version."""
    await c.roadmap.save_teacher_edit(w.id, [p.model_dump() for p in body.phases], body.expected_version)
    return await get_roadmap(w, c)


@router.post("/roadmap/phases/{phase_key}/regenerate", response_model=JobOut, status_code=202)
async def regenerate_phase(phase_key: str, body: RoadmapGenerate, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return await enqueue(s, kind="generate_roadmap", user_id=w.user_id, workspace_id=w.id,
                             payload={"instructions": body.instructions, "only_phase_key": phase_key},
                             max_attempts=2)


@router.post("/roadmap/phases/{phase_key}/status")
async def phase_status(phase_key: str, body: PhaseStatusUpdate, w: WorkspaceDep, c: ContainerDep):
    await c.roadmap.set_phase_status(w.id, phase_key, body.status)
    return {"ok": True}


@router.post("/roadmap/navigate")
async def navigate(body: NavigateRequest, w: WorkspaceDep, c: ContainerDep):
    return await c.roadmap.navigate(w.id, body.session_id, body.action, body.phase_ref)


@router.post("/roadmap/phases/{phase_key}/start", response_model=ChatTurnOut, status_code=202)
async def start_phase(phase_key: str, body: StartPhaseRequest, w: WorkspaceDep, c: ContainerDep):
    """Level-0 tutorial: make this the current phase and ask the assistant to teach it."""
    rm = await c.roadmap.current(w.id)
    ph = next((p for p in (rm.phases if rm else []) if p.key == phase_key), None)
    if ph is None:
        raise NotFoundError("Phase not found")
    idx = [p.key for p in rm.phases if p.included].index(phase_key) + 1 if ph.included else 1
    await c.roadmap.navigate(w.id, body.session_id, "goto_phase", str(idx))
    concepts = ", ".join(i.title for i in ph.items if i.included)
    text = (f"Let's start phase {idx}: {ph.title}. Teach me this phase step by step, beginning with the first "
            f"concept. Concepts in this phase: {concepts}.")
    return await post_chat_message(c, w, body.session_id, text, meta={"generated": "start_phase",
                                                                      "phase_key": phase_key})


@router.get("/coverage", response_model=list[CoverageOut])
async def coverage(w: WorkspaceDep, c: ContainerDep):
    recs = await c.coverage.records(w.id)
    return sorted(recs.values(), key=lambda r: r.concept_name.lower())


@router.post("/coverage/mark")
async def mark(body: CoverageMark, w: WorkspaceDep, c: ContainerDep):
    await c.coverage.mark(w.id, body.concept_key, body.concept_name, body.stage, body.value)
    return {"ok": True}


@router.get("/gaps")
async def gaps(w: WorkspaceDep, c: ContainerDep):
    return await c.coverage.gaps(w.id)
