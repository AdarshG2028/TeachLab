"""PostgreSQL-backed durable job queue.

- Claiming uses ``FOR UPDATE SKIP LOCKED`` so multiple workers never take the same job.
- Running jobs hold a lease; a crashed worker's job is re-claimed after the lease expires.
- ``idempotency_key`` (unique) makes enqueueing safe to repeat.
- ``pg_notify`` wakes idle workers immediately; workers also poll as a fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job

NOTIFY_CHANNEL = "teachlab_jobs"

JOB_KINDS = {
    "chat_turn", "research", "graph_extract", "ingest_url", "ingest_upload", "generate_roadmap", "generate_notes",
    "generate_assessment", "regenerate_question", "export_pdf", "rebuild_graph", "reindex_vectors",
}

TERMINAL = {"succeeded", "failed", "cancelled"}


async def enqueue(
    s: AsyncSession,
    *,
    kind: str,
    user_id: str,
    workspace_id: str | None,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    delay_seconds: float = 0,
) -> Job:
    if kind not in JOB_KINDS:
        raise ValueError(f"Unknown job kind {kind}")
    if idempotency_key:
        existing = (await s.execute(select(Job).where(Job.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing is not None:
            if existing.workspace_id != workspace_id or existing.user_id != user_id:
                raise ValueError("Idempotency key reused across scopes")
            return existing
    job = Job(kind=kind, user_id=user_id, workspace_id=workspace_id, payload=payload, max_attempts=max_attempts,
              idempotency_key=idempotency_key, progress={"percent": 0, "steps": []},
              run_after=datetime.now(UTC) + timedelta(seconds=delay_seconds))
    s.add(job)
    await s.flush()
    await s.execute(text("SELECT pg_notify(:ch, :id)"), {"ch": NOTIFY_CHANNEL, "id": job.id})
    return job


CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_by = :worker,
           lease_until = now() + make_interval(secs => :lease), started_at = coalesce(started_at, now()),
           updated_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE ((status = 'queued' AND run_after <= now())
               OR (status = 'running' AND lease_until < now()))
          AND attempts < max_attempts
        ORDER BY run_after, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)

EXPIRE_SQL = text(
    """
    UPDATE jobs SET status = 'failed', finished_at = now(), updated_at = now(),
           error = jsonb_build_object('message', 'Job exceeded max attempts (worker lost or repeated failures)',
                                      'code', 'max_attempts')
    WHERE status = 'running' AND lease_until < now() AND attempts >= max_attempts
    """
)


async def claim_next(s: AsyncSession, worker_id: str, lease_seconds: int) -> str | None:
    await s.execute(EXPIRE_SQL)
    return (await s.execute(CLAIM_SQL, {"worker": worker_id, "lease": lease_seconds})).scalar_one_or_none()


async def cancel(s: AsyncSession, job: Job) -> None:
    if job.status in ("queued",):
        await s.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                        .values(status="cancelled", finished_at=datetime.now(UTC)))
