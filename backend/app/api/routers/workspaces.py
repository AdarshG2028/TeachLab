"""Workspaces, overview/resume, research sessions and chat."""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import ContainerDep, PrincipalDep, WorkspaceDep
from app.api.schemas import (
    ChatTurnOut,
    JobOut,
    MessageCreate,
    MessageOut,
    SessionCreate,
    SessionOut,
    WorkflowStep,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceOverview,
    WorkspaceUpdate,
)
from app.core.errors import NotFoundError
from app.db.models import Assessment, Job, NotesDocument, ResearchSession, RoadmapVersion, Source, Workspace
from app.jobs.queue import enqueue
from app.services.conversation.memory import DEFAULT_STATE, append_message

router = APIRouter(tags=["workspaces"])


@router.get("/health", tags=["system"])
async def health(c: ContainerDep) -> dict:
    checks: dict[str, str] = {}
    try:
        async with c.db.session() as s:
            await s.execute(select(1))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        await c.graph.ping()
        checks["neo4j"] = "ok"
    except Exception as exc:
        checks["neo4j"] = f"error: {type(exc).__name__}"
    try:
        await c.vectors.ensure_collection()
        checks["qdrant"] = "ok"
    except Exception as exc:
        checks["qdrant"] = f"error: {type(exc).__name__}"
    return {"status": "ok" if all(v == "ok" for v in checks.values()) else "degraded", "checks": checks,
            "providers": {"llm": c.settings.llm_provider, "search": c.settings.search_provider,
                          "embedding_model": c.settings.embedding_model, "pdf": "reportlab",
                          "auth": c.settings.auth_provider}}


@router.get("/me", tags=["system"])
async def me(principal: PrincipalDep) -> dict:
    return {"user_id": principal.user_id, "display_name": principal.display_name}


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(principal: PrincipalDep, c: ContainerDep, include_archived: bool = False):
    async with c.db.session() as s:
        q = select(Workspace).where(Workspace.owner_id == principal.user_id)
        if not include_archived:
            q = q.where(Workspace.archived.is_(False))
        return (await s.execute(q.order_by(Workspace.updated_at.desc()))).scalars().all()


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201)
async def create_workspace(body: WorkspaceCreate, principal: PrincipalDep, c: ContainerDep):
    async with c.db.session() as s:
        ws = Workspace(owner_id=principal.user_id, title=body.title or body.topic, topic=body.topic,
                       description=body.description, audience=body.audience, academic_level=body.academic_level,
                       teaching_goals=body.teaching_goals, familiarity_level=body.familiarity_level,
                       time_available_minutes=body.time_available_minutes, language=body.language,
                       content_preferences=body.content_preferences)
        s.add(ws)
        await s.flush()
        s.add(ResearchSession(workspace_id=ws.id, title="Research session", state=dict(DEFAULT_STATE)))
        await s.flush()
        return ws


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(w: WorkspaceDep):
    return w.workspace


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(body: WorkspaceUpdate, w: WorkspaceDep, c: ContainerDep):
    """Changing familiarity_level switches the default workflow; no research is lost."""
    async with c.db.session() as s:
        ws = await s.get(Workspace, w.id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(ws, k, v)
        await s.flush()
        await s.refresh(ws)
        return ws


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(w: WorkspaceDep, c: ContainerDep):
    await c.graph.delete_workspace(w.id)
    await c.vectors.delete_workspace(w.id)
    async with c.db.session() as s:
        ws = await s.get(Workspace, w.id)
        await s.delete(ws)


def workflow_for(level: int, has: dict[str, bool]) -> list[WorkflowStep]:
    """Default workflow per familiarity level. These are defaults, not restrictions."""
    def st(done: bool, default: str) -> str:
        return "done" if done else default

    research = WorkflowStep(key="research", label="Research the topic",
                            status=st(has["sources"], "recommended" if level < 2 else "available"))
    roadmap = WorkflowStep(key="roadmap", label="Generate roadmap",
                           status=st(has["roadmap"], "recommended" if level < 2 else "optional"))
    tutorial = WorkflowStep(key="tutorial", label="Learn phase by phase",
                            status=st(has["phases_done"], "recommended" if level == 0 else "optional"))
    notes = WorkflowStep(key="notes", label="Generate teacher & student notes",
                         status=st(has["notes"], "recommended" if level >= 1 or has["phases_done"] else "available"))
    test = WorkflowStep(key="assessment", label="Configure & generate a test paper",
                        status=st(has["assessment"], "recommended" if level >= 1 or has["notes"] else "available"))
    export = WorkflowStep(key="export", label="Download PDFs", status=st(False, "available"))
    return [research, roadmap, tutorial, notes, test, export]


@router.get("/workspaces/{workspace_id}/overview", response_model=WorkspaceOverview)
async def overview(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        sources = (await s.execute(select(func.count()).select_from(Source).where(Source.workspace_id == w.id))
                   ).scalar_one()
        latest = (await s.execute(select(ResearchSession).where(ResearchSession.workspace_id == w.id)
                                  .order_by(ResearchSession.updated_at.desc()).limit(1))).scalar_one_or_none()
        rm = (await s.execute(select(RoadmapVersion).where(RoadmapVersion.workspace_id == w.id,
                                                           RoadmapVersion.is_current))).scalar_one_or_none()
        notes = (await s.execute(select(func.count()).select_from(NotesDocument).where(
            NotesDocument.workspace_id == w.id))).scalar_one()
        assessments = (await s.execute(select(func.count()).select_from(Assessment).where(
            Assessment.workspace_id == w.id, Assessment.status == "ready"))).scalar_one()
        active = (await s.execute(select(Job).where(Job.workspace_id == w.id,
                                                    Job.status.in_(["queued", "running"]))
                                  .order_by(Job.created_at.desc()).limit(20))).scalars().all()
    try:
        graph_stats = await c.graph.stats(w.id)
    except Exception:
        graph_stats = {}
    state = (latest.state if latest else {}) or {}
    has = {"sources": sources > 0, "roadmap": rm is not None, "notes": notes > 0, "assessment": assessments > 0,
           "phases_done": bool(state.get("completed_phase_ids"))}
    return WorkspaceOverview(
        workspace=WorkspaceOut.model_validate(w.workspace),
        stats={"sources": sources, "notes": notes, "assessments": assessments, "graph": graph_stats},
        latest_session_id=latest.id if latest else None, roadmap_version=rm.version if rm else None,
        workflow=workflow_for(w.workspace.familiarity_level, has),
        active_jobs=[JobOut.model_validate(j) for j in active])


@router.get("/workspaces/{workspace_id}/sessions", response_model=list[SessionOut])
async def list_sessions(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return (await s.execute(select(ResearchSession).where(ResearchSession.workspace_id == w.id)
                                .order_by(ResearchSession.updated_at.desc()))).scalars().all()


@router.post("/workspaces/{workspace_id}/sessions", response_model=SessionOut, status_code=201)
async def create_session(body: SessionCreate, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        sess = ResearchSession(workspace_id=w.id, title=body.title, state=dict(DEFAULT_STATE))
        s.add(sess)
        await s.flush()
        await s.refresh(sess)
        return sess


async def _session(c, w, session_id: str) -> ResearchSession:
    async with c.db.session() as s:
        sess = await s.get(ResearchSession, session_id)
    if sess is None or sess.workspace_id != w.id:
        raise NotFoundError("Session not found")
    return sess


@router.get("/workspaces/{workspace_id}/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: str, w: WorkspaceDep, c: ContainerDep):
    return await _session(c, w, session_id)


@router.get("/workspaces/{workspace_id}/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: str, w: WorkspaceDep, c: ContainerDep,
                        before_seq: int | None = None, limit: int = Query(default=50, ge=1, le=200)):
    """Full history is stored; page backwards with ``before_seq``."""
    from app.db.models import Message

    await _session(c, w, session_id)
    async with c.db.session() as s:
        q = select(Message).where(Message.session_id == session_id)
        if before_seq is not None:
            q = q.where(Message.seq < before_seq)
        rows = (await s.execute(q.order_by(Message.seq.desc()).limit(limit))).scalars().all()
    return list(reversed(rows))


async def post_chat_message(c, w, session_id: str, content: str, meta: dict | None = None) -> ChatTurnOut:
    sess = await _session(c, w, session_id)
    async with c.db.session() as s:
        msg = await append_message(s, sess, "user", content, meta=meta or {})
        job = await enqueue(s, kind="chat_turn", user_id=w.user_id, workspace_id=w.id,
                            payload={"session_id": session_id, "message_id": msg.id},
                            idempotency_key=f"chat_turn:{msg.id}", max_attempts=3)
        return ChatTurnOut(message=MessageOut.model_validate(msg), job=JobOut.model_validate(job))


@router.post("/workspaces/{workspace_id}/sessions/{session_id}/messages", response_model=ChatTurnOut,
             status_code=202)
async def send_message(session_id: str, body: MessageCreate, w: WorkspaceDep, c: ContainerDep):
    """Stores the teacher's message and queues the assistant turn; follow the job for progress."""
    return await post_chat_message(c, w, session_id, body.content)
