"""Job status, cancellation and Server-Sent Events progress stream."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import ContainerDep, PrincipalDep, WorkspaceDep
from app.api.schemas import JobOut
from app.core.errors import NotFoundError
from app.db.models import Job
from app.jobs.queue import TERMINAL, cancel

router = APIRouter(tags=["jobs"])


async def _job(c, principal, job_id: str) -> Job:
    async with c.db.session() as s:
        job = await s.get(Job, job_id)
    if job is None or job.user_id != principal.user_id:
        raise NotFoundError("Job not found")
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, principal: PrincipalDep, c: ContainerDep):
    return await _job(c, principal, job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, principal: PrincipalDep, c: ContainerDep):
    job = await _job(c, principal, job_id)
    async with c.db.session() as s:
        await cancel(s, job)
    return await _job(c, principal, job_id)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, principal: PrincipalDep, c: ContainerDep):
    """SSE stream: emits a ``job`` event whenever the job row changes, ends at a terminal state.

    Works across processes because it observes the durable job row. Clients without SSE can poll GET /jobs/{id}.
    """
    await _job(c, principal, job_id)

    async def stream():
        last = None
        for _ in range(60 * 30):  # max ~30 minutes
            if await request.is_disconnected():
                return
            job = await _job(c, principal, job_id)
            snapshot = JobOut.model_validate(job).model_dump(mode="json")
            marker = (job.status, json.dumps(job.progress, sort_keys=True, default=str))
            if marker != last:
                last = marker
                yield {"event": "job", "data": json.dumps(snapshot)}
            if job.status in TERMINAL:
                return
            await asyncio.sleep(1.0)

    return EventSourceResponse(stream(), ping=15)


@router.get("/workspaces/{workspace_id}/jobs", response_model=list[JobOut])
async def workspace_jobs(w: WorkspaceDep, c: ContainerDep, active_only: bool = False, limit: int = 50):
    async with c.db.session() as s:
        q = select(Job).where(Job.workspace_id == w.id, Job.user_id == w.user_id)
        if active_only:
            q = q.where(Job.status.in_(["queued", "running"]))
        return (await s.execute(q.order_by(Job.created_at.desc()).limit(min(limit, 200)))).scalars().all()
