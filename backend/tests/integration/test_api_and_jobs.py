"""API authorization/isolation, session persistence, durable jobs, retries and provider failures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.errors import ProviderError
from app.db.models import Job
from app.jobs.queue import enqueue
from app.worker.runner import Worker
from tests.conftest import create_workspace, job_ok, run_jobs

pytestmark = pytest.mark.integration
B = {"X-User-Id": "teacher-b"}


async def test_workspace_crud_and_mode_switch(client):
    ws = await create_workspace(client, "Cloud computing", level=2)
    r = await client.get("/api/workspaces")
    assert [w["id"] for w in r.json()] == [ws["id"]]
    r = await client.patch(f"/api/workspaces/{ws['id']}", json={"familiarity_level": 0})
    assert r.status_code == 200 and r.json()["familiarity_level"] == 0
    ov = (await client.get(f"/api/workspaces/{ws['id']}/overview")).json()
    assert ov["latest_session_id"] and ov["workflow"][2]["key"] == "tutorial"
    assert ov["workflow"][2]["status"] == "recommended"  # level 0 recommends the tutorial
    r = await client.post("/api/workspaces", json={"topic": "x"})
    assert r.status_code == 422


async def test_cross_user_access_is_denied_everywhere(client):
    ws = await create_workspace(client, "Quantum computing")
    wid = ws["id"]
    session_id = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
    msg = await client.post(f"/api/workspaces/{wid}/sessions/{session_id}/messages", json={"content": "hi"})
    job_id = msg.json()["job"]["id"]
    routes = [
        ("get", f"/api/workspaces/{wid}"), ("patch", f"/api/workspaces/{wid}"),
        ("delete", f"/api/workspaces/{wid}"), ("get", f"/api/workspaces/{wid}/overview"),
        ("get", f"/api/workspaces/{wid}/sessions"), ("get", f"/api/workspaces/{wid}/sessions/{session_id}/messages"),
        ("post", f"/api/workspaces/{wid}/sessions/{session_id}/messages"), ("post", f"/api/workspaces/{wid}/research"),
        ("get", f"/api/workspaces/{wid}/sources"), ("get", f"/api/workspaces/{wid}/graph/overview"),
        ("get", f"/api/workspaces/{wid}/graph/search?q=a"), ("get", f"/api/workspaces/{wid}/ontology/proposals"),
        ("get", f"/api/workspaces/{wid}/roadmap"), ("post", f"/api/workspaces/{wid}/roadmap/generate"),
        ("get", f"/api/workspaces/{wid}/coverage"), ("get", f"/api/workspaces/{wid}/gaps"),
        ("get", f"/api/workspaces/{wid}/notes"), ("post", f"/api/workspaces/{wid}/notes/generate"),
        ("get", f"/api/workspaces/{wid}/assessments"), ("post", f"/api/workspaces/{wid}/exports"),
        ("get", f"/api/workspaces/{wid}/artifacts"), ("get", f"/api/workspaces/{wid}/jobs"),
        ("get", f"/api/jobs/{job_id}"), ("get", f"/api/jobs/{job_id}/events"),
    ]
    for method, url in routes:
        kwargs = {"headers": B}
        if method in ("post", "patch"):
            kwargs["json"] = {"content": "x", "queries": ["x"], "kind": "student_notes", "title": "t"}
        r = await getattr(client, method)(url, **kwargs)
        assert r.status_code in (404, 422), (method, url, r.status_code)
        if r.status_code == 422:  # validation may run first; it must never succeed
            pass
    # teacher B sees nothing in listings
    assert (await client.get("/api/workspaces", headers=B)).json() == []
    # and the workspace is untouched for its owner
    assert (await client.get(f"/api/workspaces/{wid}")).status_code == 200


async def test_invalid_user_header_rejected(client):
    r = await client.get("/api/workspaces", headers={"X-User-Id": "bad id with spaces"})
    assert r.status_code == 401


async def test_session_persistence_and_resume(client, worker):
    ws = await create_workspace(client, "IoT sensors", level=1)
    wid = ws["id"]
    s2 = (await client.post(f"/api/workspaces/{wid}/sessions", json={"title": "Second"})).json()
    for text in ["What does a temperature sensor measure in IoT?", "Thanks, and what is MQTT?"]:
        r = await client.post(f"/api/workspaces/{wid}/sessions/{s2['id']}/messages", json={"content": text})
        assert r.status_code == 202
        await run_jobs(worker)
        await job_ok(client, r.json()["job"]["id"])
    msgs = (await client.get(f"/api/workspaces/{wid}/sessions/{s2['id']}/messages")).json()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m["seq"] for m in msgs] == [1, 2, 3, 4]
    older = (await client.get(f"/api/workspaces/{wid}/sessions/{s2['id']}/messages?before_seq=3")).json()
    assert [m["seq"] for m in older] == [1, 2]
    # resume: overview points at the most recently active session
    ov = (await client.get(f"/api/workspaces/{wid}/overview")).json()
    assert ov["latest_session_id"] == s2["id"]


async def test_job_idempotency_key(container):
    async with container.db.session() as s:
        from app.db.models import User, Workspace

        s.add(User(id="u1"))
        await s.flush()
        ws = Workspace(owner_id="u1", title="t", topic="t")
        s.add(ws)
        await s.flush()
        j1 = await enqueue(s, kind="rebuild_graph", user_id="u1", workspace_id=ws.id, payload={},
                           idempotency_key="k-1")
        j2 = await enqueue(s, kind="rebuild_graph", user_id="u1", workspace_id=ws.id, payload={},
                           idempotency_key="k-1")
    assert j1.id == j2.id


async def test_retry_backoff_then_failure_is_recorded(container, client):
    ws = await create_workspace(client, "Quantum")
    attempts = {"n": 0}

    async def flaky(ctx, payload):
        attempts["n"] += 1
        raise ProviderError("Groq rate limited", retryable=True, retry_after=1)

    async def fatal(ctx, payload):
        raise ProviderError("Invalid API key", retryable=False)

    w = Worker(container, {"research": flaky, "rebuild_graph": fatal}, worker_id="t")
    async with container.db.session() as s:
        j_flaky = await enqueue(s, kind="research", user_id="teacher-a", workspace_id=ws["id"], payload={},
                                max_attempts=3)
        j_fatal = await enqueue(s, kind="rebuild_graph", user_id="teacher-a", workspace_id=ws["id"], payload={})
    for _ in range(3):
        await w.run_until_idle()
        async with container.db.session() as s:  # fast-forward the backoff
            await s.execute(update(Job).where(Job.id == j_flaky.id, Job.status == "queued")
                            .values(run_after=datetime.now(UTC) - timedelta(seconds=1)))
    async with container.db.session() as s:
        jf = await s.get(Job, j_flaky.id)
        jx = await s.get(Job, j_fatal.id)
    assert attempts["n"] == 3 and jf.status == "failed" and jf.error["retryable"] is True
    assert jx.status == "failed" and jx.attempts == 1 and jx.error["message"] == "Invalid API key"


async def test_crashed_worker_lease_is_reclaimed(container, client):
    ws = await create_workspace(client, "Quantum")
    done = []

    async def ok(ctx, payload):
        done.append(ctx.job_id)
        return {"ok": True}

    async with container.db.session() as s:
        job = await enqueue(s, kind="research", user_id="teacher-a", workspace_id=ws["id"], payload={})
        # simulate a worker that claimed the job and died
        await s.execute(update(Job).where(Job.id == job.id).values(
            status="running", attempts=1, locked_by="dead-worker",
            lease_until=datetime.now(UTC) - timedelta(seconds=5)))
    await Worker(container, {"research": ok}, worker_id="alive").run_until_idle()
    async with container.db.session() as s:
        j = (await s.execute(select(Job).where(Job.id == job.id))).scalar_one()
    assert done == [job.id] and j.status == "succeeded" and j.attempts == 2


async def test_job_owner_recheck_in_worker(container, client):
    """A job whose workspace is not owned by the job user fails instead of touching data."""
    ws = await create_workspace(client, "Quantum")
    async with container.db.session() as s:
        from app.db.models import User

        s.add(User(id="intruder"))
        await s.flush()
        job = await enqueue(s, kind="rebuild_graph", user_id="intruder", workspace_id=ws["id"], payload={})
    from app.worker.handlers import HANDLERS

    await Worker(container, HANDLERS, worker_id="t").run_until_idle()
    async with container.db.session() as s:
        j = await s.get(Job, job.id)
    assert j.status == "failed" and "not found" in j.error["message"].lower()


async def test_llm_invalid_output_is_repaired(client, worker, fake_llm):
    ws = await create_workspace(client, "Quantum computing")
    sid = (await client.get(f"/api/workspaces/{ws['id']}/overview")).json()["latest_session_id"]
    fake_llm.script("plan_turn", "this is not json")  # first attempt invalid -> repair loop
    r = await client.post(f"/api/workspaces/{ws['id']}/sessions/{sid}/messages",
                          json={"content": "Please research qubits"})
    await run_jobs(worker)
    await job_ok(client, r.json()["job"]["id"])
    plan_calls = [c for c in fake_llm.calls if c["task"] == "plan_turn"]
    assert len(plan_calls) == 2
    assert "previous output was invalid" in plan_calls[1]["messages"][-1].content


async def test_ssrf_url_rejected_at_api(client):
    ws = await create_workspace(client, "Quantum")
    r = await client.post(f"/api/workspaces/{ws['id']}/sources/url", json={"url": "http://169.254.169.254/x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsafe_url"


async def test_upload_ingestion(client, worker):
    ws = await create_workspace(client, "IoT")
    content = ("# MQTT notes\n\nMQTT is a lightweight messaging protocol. Each IoT device uses MQTT. "
               "A temperature sensor measures temperature. " * 5).encode()
    r = await client.post(f"/api/workspaces/{ws['id']}/sources/upload",
                          files={"file": ("mqtt-notes.md", content, "text/markdown")})
    assert r.status_code == 202, r.text
    await run_jobs(worker)
    job = await job_ok(client, r.json()["id"])
    assert job["result"]["created"] and job["result"]["graph"]["claims"] >= 1
    r2 = await client.post(f"/api/workspaces/{ws['id']}/sources/upload",
                           files={"file": ("evil.exe", b"MZ\x00\x00", "application/octet-stream")})
    assert r2.status_code == 422
    sources = (await client.get(f"/api/workspaces/{ws['id']}/sources")).json()
    assert len(sources) == 1 and sources[0]["origin"] == "upload"
