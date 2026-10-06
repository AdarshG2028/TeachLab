"""No-sign-in identity, usage caps, Postgres blob storage and deployment-oriented settings."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.container import Container
from app.core.config import Settings
from app.jobs import queue
from app.providers.storage.postgres import PostgresStorage
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def anon_client(container: Container, tmp_root):
    """App configured exactly like a public deployment: anonymous auth, caps on."""
    from app.main import create_app

    settings = make_settings(tmp_root, env="test", auth_provider="anonymous", daily_job_limit_per_user=3,
                             max_workspaces_per_user=2)
    saved = (container.auth, container.settings)
    container.auth = Container(settings, db=container.db).auth
    container.settings = settings
    queue.QUOTA.per_user, queue.QUOTA.global_ = 3, 0
    app = create_app(settings, container=container)
    app.state.container = container
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c
    finally:  # the container is shared by the whole test session
        container.auth, container.settings = saved
        queue.QUOTA.per_user = queue.QUOTA.global_ = 0


def hdr(cid: str) -> dict:
    return {"X-Client-Id": cid}


async def test_anonymous_identity_isolates_browsers(anon_client):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    r = await anon_client.post("/api/workspaces", json={"topic": "Quantum computing"}, headers=hdr(a))
    assert r.status_code == 201
    wid = r.json()["id"]
    assert (await anon_client.get(f"/api/workspaces/{wid}", headers=hdr(a))).status_code == 200
    assert (await anon_client.get(f"/api/workspaces/{wid}", headers=hdr(b))).status_code == 404
    assert (await anon_client.get("/api/workspaces", headers=hdr(b))).json() == []
    # the id can also travel as a query parameter (EventSource / download links)
    assert (await anon_client.get(f"/api/workspaces/{wid}?client_id={a}")).status_code == 200


@pytest.mark.parametrize("bad", ["", "teacher-a", "123", "../../etc", "' OR 1=1 --", str(uuid.uuid4()) + "x"])
async def test_anonymous_rejects_missing_or_malformed_ids(anon_client, bad):
    assert (await anon_client.get("/api/workspaces", headers={"X-Client-Id": bad})).status_code == 401
    assert (await anon_client.get("/api/workspaces")).status_code == 401
    # the dev header is ignored in anonymous mode
    assert (await anon_client.get("/api/workspaces", headers={"X-User-Id": "teacher-a"})).status_code == 401


async def test_workspace_and_daily_job_caps(anon_client):
    cid = str(uuid.uuid4())
    for t in ("One", "Two"):
        assert (await anon_client.post("/api/workspaces", json={"topic": t + " topic"}, headers=hdr(cid))).status_code == 201
    r = await anon_client.post("/api/workspaces", json={"topic": "Three topic"}, headers=hdr(cid))
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"

    wid = (await anon_client.get("/api/workspaces", headers=hdr(cid))).json()[0]["id"]
    for i in range(3):
        r = await anon_client.post(f"/api/workspaces/{wid}/research", json={"queries": [f"q{i}"]}, headers=hdr(cid))
        assert r.status_code == 202
    r = await anon_client.post(f"/api/workspaces/{wid}/research", json={"queries": ["q4"]}, headers=hdr(cid))
    assert r.status_code == 429 and "Daily limit" in r.json()["error"]["message"]
    # another browser has its own allowance
    other = str(uuid.uuid4())
    w2 = (await anon_client.post("/api/workspaces", json={"topic": "Other topic"}, headers=hdr(other))).json()["id"]
    assert (await anon_client.post(f"/api/workspaces/{w2}/research", json={"queries": ["x"]},
                                   headers=hdr(other))).status_code == 202


async def test_postgres_blob_storage_roundtrip(container: Container):
    store = PostgresStorage(container.db)
    key = "ws-1/artifacts/test-doc.pdf"
    assert not await store.exists(key)
    await store.put(key, b"%PDF-1 first", "application/pdf")
    await store.put(key, b"%PDF-1 second version", "application/pdf")  # overwrite
    assert await store.get(key) == b"%PDF-1 second version" and await store.exists(key)
    await store.delete(key)
    assert not await store.exists(key)
    from app.core.errors import NotFoundError, ValidationFailed

    with pytest.raises(NotFoundError):
        await store.get(key)
    for bad in ("../secret", "a/../../b", "has space", ""):
        with pytest.raises(ValidationFailed):
            await store.put(bad, b"x")


async def test_full_export_with_postgres_storage(client, worker, container: Container):
    """Artifacts are written to and downloaded from PostgreSQL (the production storage backend)."""
    from tests.conftest import create_workspace, job_ok, run_jobs

    saved_storage = container.storage
    container.storage = PostgresStorage(container.db)
    try:
        await _export_roundtrip(client, worker, create_workspace, job_ok, run_jobs)
    finally:
        container.storage = saved_storage


async def _export_roundtrip(client, worker, create_workspace, job_ok, run_jobs):

    ws = await create_workspace(client, "quantum computing qubits", level=1)
    wid = ws["id"]
    r = await client.post(f"/api/workspaces/{wid}/research", json={"queries": ["quantum computing qubits"]})
    await run_jobs(worker)
    await job_ok(client, r.json()["id"])
    r = await client.post(f"/api/workspaces/{wid}/notes/generate", json={"kind": "student"})
    await run_jobs(worker)
    await job_ok(client, r.json()["id"])
    r = await client.post(f"/api/workspaces/{wid}/exports", json={"kind": "student_notes"})
    await run_jobs(worker)
    art = (await job_ok(client, r.json()["id"]))["result"]["artifact_id"]
    pdf = await client.get(f"/api/workspaces/{wid}/artifacts/{art}/download")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


@pytest.mark.nodb
def test_settings_normalize_provider_urls_and_origins():
    s = Settings(env="test", database_url="postgres://u:p@host:5432/db?sslmode=require",
                 cors_origins="https://a.vercel.app/, https://b.example.com")
    assert s.database_url == "postgresql+asyncpg://u:p@host:5432/db?ssl=require"
    assert s.cors_origins == ["https://a.vercel.app", "https://b.example.com"]
    assert Settings(env="test", cors_origins='["https://x.io"]').cors_origins == ["https://x.io"]
    assert Settings(env="test", database_url="postgresql://u:p@h/d").database_url.startswith("postgresql+asyncpg://")


@pytest.mark.nodb
def test_production_allows_anonymous_but_not_local():
    Settings(env="production", auth_provider="anonymous", llm_provider="groq", search_provider="exa")
    with pytest.raises(ValueError):
        Settings(env="production", auth_provider="local")
