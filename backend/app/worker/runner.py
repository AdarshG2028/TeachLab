"""Job worker: claims jobs from PostgreSQL and runs registered handlers.

Run with ``python -m app.worker``. Safe to run several instances.
"""

from __future__ import annotations

import asyncio
import os
import socket
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog
from sqlalchemy import select, update

from app.container import Container
from app.core.errors import AppError, ProviderError
from app.core.logging import get_logger
from app.db.models import Job, Workspace
from app.jobs.queue import NOTIFY_CHANNEL, claim_next

log = get_logger(__name__)


class JobContext:
    def __init__(self, container: Container, job: Job) -> None:
        self.container = container
        self.job = job
        self.job_id = job.id
        self.user_id = job.user_id
        self.workspace_id = job.workspace_id
        self._steps: list[dict[str, Any]] = list((job.progress or {}).get("steps", []))

    async def progress(self, step: str, percent: int, detail: str | None = None) -> None:
        entry = {"step": step, "percent": max(0, min(100, percent)), "at": datetime.now(UTC).isoformat()}
        if detail:
            entry["detail"] = detail[:500]
        self._steps.append(entry)
        async with self.container.db.session() as s:
            await s.execute(update(Job).where(Job.id == self.job_id).values(
                progress={"percent": entry["percent"], "current": step, "steps": self._steps[-40:]}))
        log.info("job_progress", step=step, percent=percent)

    async def workspace(self) -> Workspace:
        """Defense in depth: re-check that the job's workspace belongs to the job's user."""
        async with self.container.db.session() as s:
            ws = await s.get(Workspace, self.workspace_id)
        if ws is None or ws.owner_id != self.user_id:
            raise AppError("Workspace not found for job owner")
        return ws


Handler = Callable[[JobContext, dict[str, Any]], Awaitable[dict[str, Any]]]


def backoff_seconds(attempt: int, hint: float | None = None) -> float:
    if hint is not None:
        return min(600.0, max(1.0, hint))
    return min(600.0, 5.0 * 3 ** (attempt - 1))


class Worker:
    def __init__(self, container: Container, handlers: dict[str, Handler], *, concurrency: int | None = None,
                 worker_id: str | None = None) -> None:
        self.container = container
        self.handlers = handlers
        self.settings = container.settings
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._running: set[asyncio.Task] = set()

    async def _listen(self) -> asyncpg.Connection | None:
        dsn = self.settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        try:
            conn = await asyncpg.connect(dsn)
            await conn.add_listener(NOTIFY_CHANNEL, lambda *_: self._wake.set())
            return conn
        except Exception as exc:  # polling still works without LISTEN
            log.warning("worker_listen_failed", error=str(exc))
            return None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        log.info("worker_started", worker=self.worker_id, concurrency=self.concurrency, kinds=sorted(self.handlers))
        conn = await self._listen()
        try:
            while not self._stop.is_set():
                while len(self._running) < self.concurrency and not self._stop.is_set():
                    job_id = await self._claim()
                    if not job_id:
                        break
                    task = asyncio.create_task(self._execute(job_id))
                    self._running.add(task)
                    task.add_done_callback(self._running.discard)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.settings.worker_poll_seconds)
                except TimeoutError:
                    pass
            if self._running:
                await asyncio.gather(*self._running, return_exceptions=True)
        finally:
            if conn is not None:
                await conn.close()
            log.info("worker_stopped", worker=self.worker_id)

    async def run_until_idle(self, max_jobs: int = 1000) -> int:
        """Process jobs sequentially until none are runnable (used by tests and the demo script)."""
        done = 0
        while done < max_jobs:
            job_id = await self._claim()
            if not job_id:
                break
            await self._execute(job_id)
            done += 1
        return done

    async def _claim(self) -> str | None:
        async with self.container.db.session() as s:
            return await claim_next(s, self.worker_id, self.settings.worker_lease_seconds)

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(5, self.settings.worker_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            async with self.container.db.session() as s:
                await s.execute(update(Job).where(Job.id == job_id, Job.locked_by == self.worker_id).values(
                    lease_until=datetime.now(UTC) + timedelta(seconds=self.settings.worker_lease_seconds)))

    async def _execute(self, job_id: str) -> None:
        async with self.container.db.session() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        structlog.contextvars.bind_contextvars(job_id=job.id, job_kind=job.kind, workspace_id=job.workspace_id)
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        try:
            handler = self.handlers.get(job.kind)
            if handler is None:
                raise AppError(f"No handler for job kind '{job.kind}'")
            ctx = JobContext(self.container, job)
            result = await handler(ctx, dict(job.payload or {}))
            async with self.container.db.session() as s:
                await s.execute(update(Job).where(Job.id == job.id).values(
                    status="succeeded", result=result or {}, finished_at=datetime.now(UTC), lease_until=None,
                    error={}, progress={"steps": ctx._steps[-40:], "percent": 100, "current": "done"}))
            log.info("job_succeeded")
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded on the job
            await self._fail(job, exc)
        finally:
            heartbeat.cancel()
            structlog.contextvars.clear_contextvars()

    async def _fail(self, job: Job, exc: Exception) -> None:
        retryable = isinstance(exc, ProviderError) and exc.retryable
        hint = exc.retry_after if isinstance(exc, ProviderError) else None
        error = {"message": getattr(exc, "message", str(exc))[:2000], "type": type(exc).__name__,
                 "code": getattr(exc, "code", "internal_error"), "retryable": retryable,
                 "attempt": job.attempts}
        if not isinstance(exc, AppError):
            error["trace"] = traceback.format_exc()[-3000:]
        async with self.container.db.session() as s:
            current = (await s.execute(select(Job.attempts, Job.max_attempts).where(Job.id == job.id))).one()
            if retryable and current.attempts < current.max_attempts:
                await s.execute(update(Job).where(Job.id == job.id).values(
                    status="queued", lease_until=None, locked_by=None, error=error,
                    run_after=datetime.now(UTC) + timedelta(seconds=backoff_seconds(current.attempts, hint))))
                log.warning("job_retry_scheduled", error=error["message"][:300])
            else:
                await s.execute(update(Job).where(Job.id == job.id).values(
                    status="failed", finished_at=datetime.now(UTC), lease_until=None, error=error))
                log.error("job_failed", error=error["message"][:500])
