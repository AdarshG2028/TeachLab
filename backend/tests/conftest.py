"""Shared fixtures.

Integration tests use the docker-compose Postgres test DB plus an in-memory Qdrant, the real local BGE model, the deterministic FakeLLMProvider and the
fixture search provider. Each test starts from empty tables.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app.container import Container
from app.core.config import BACKEND_DIR, Settings
from app.db.models import Base
from app.providers.llm.fake import FakeLLMProvider
from app.worker.handlers import HANDLERS
from app.worker.runner import Worker

TEST_DB = os.environ.get("TEST_DATABASE_URL",
                         "postgresql+asyncpg://teachlab:teachlab@localhost:5432/teachlab_test")


def make_settings(tmp: Path, **overrides) -> Settings:
    base = dict(
        env="test", auth_provider="local", database_url=TEST_DB, llm_provider="fake", search_provider="fixture", qdrant_url="",
        qdrant_path=":memory:", storage_dir=tmp / "storage", worker_poll_seconds=0.2, log_level="WARNING",
        search_fixture_dir=BACKEND_DIR / "tests" / "fixtures" / "search", summary_trigger_tokens=150,
        context_recent_messages=4, retrieval_min_score=0.5,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="session")
def tmp_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("teachlab")


@pytest.fixture(scope="session")
async def container(tmp_root) -> AsyncIterator[Container]:
    c = Container(make_settings(tmp_root))
    c._llm = FakeLLMProvider()
    await c.vectors.ensure_collection()
    yield c
    await c.close()


@pytest.fixture(autouse=True)
async def clean_state(request) -> AsyncIterator[None]:
    if "nodb" in request.keywords:
        yield
        return
    container: Container = request.getfixturevalue("container")
    yield
    async with container.db.session() as s:
        ids = (await s.execute(text("SELECT id FROM workspaces"))).scalars().all()
        for wid in ids:
            await container.vectors.delete_workspace(wid)
        tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        await s.execute(text(f"TRUNCATE {tables} CASCADE"))
    fake = container._llm
    if isinstance(fake, FakeLLMProvider):
        fake.calls.clear()
        fake._scripts.clear()
        fake.request_limit = 32_000


@pytest.fixture
def fake_llm(container: Container) -> FakeLLMProvider:
    return container._llm  # type: ignore[return-value]


@pytest.fixture
def worker(container: Container) -> Worker:
    return Worker(container, HANDLERS, concurrency=1, worker_id="test-worker")


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import create_app

    app = create_app(container.settings, container=container)
    app.state.container = container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers={"X-User-Id": "teacher-a"}) as c:
        yield c


async def create_workspace(client: httpx.AsyncClient, topic: str, level: int = 1, user: str = "teacher-a",
                           **extra) -> dict:
    body = {"topic": topic, "familiarity_level": level, "audience": "high school students",
            "academic_level": "grade 11", "teaching_goals": ["understand core ideas"], **extra}
    r = await client.post("/api/workspaces", json=body, headers={"X-User-Id": user})
    assert r.status_code == 201, r.text
    return r.json()


async def run_jobs(worker: Worker) -> int:
    return await worker.run_until_idle()


async def job_ok(client: httpx.AsyncClient, job_id: str, user: str = "teacher-a") -> dict:
    r = await client.get(f"/api/jobs/{job_id}", headers={"X-User-Id": user})
    job = r.json()
    assert job["status"] == "succeeded", job
    return job
