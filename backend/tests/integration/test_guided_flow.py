"""Guided beginner flow: roadmap first -> one lesson per phase on "next" -> teaching pack at the end."""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from pypdf import PdfReader

from app.core.retry import TokenBucket
from app.jobs.queue import claim_next, enqueue
from app.services.tutor.guide import quick_intent
from tests.conftest import create_workspace, job_ok, run_jobs

pytestmark = pytest.mark.integration


async def say(client, worker, wid, sid, text) -> dict:
    r = await client.post(f"/api/workspaces/{wid}/sessions/{sid}/messages", json={"content": text})
    assert r.status_code == 202, r.text
    await run_jobs(worker)
    await job_ok(client, r.json()["job"]["id"])
    return (await client.get(f"/api/workspaces/{wid}/sessions/{sid}/messages?limit=1")).json()[-1]


@pytest.mark.nodb
def test_quick_intents():
    assert quick_intent("next") == "next" and quick_intent("Next phase!") == "next" and quick_intent("ok, next") == "next"
    assert quick_intent("start") == "start" and quick_intent("let's begin") == "start"
    assert quick_intent("generate teaching pack") == "pack"
    assert quick_intent("please generate the notes and the test paper") == "pack"
    assert quick_intent("what comes next in the Krebs cycle?") is None
    assert quick_intent("start of the war") is None


async def test_guided_beginner_flow_end_to_end(client, worker, fake_llm):
    ws = await create_workspace(client, "quantum computing qubits", level=0)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]

    # 1. "start": research + roadmap first, shown in chat; no lesson yet and no planner call needed
    msg = await say(client, worker, wid, sid, "start")
    guided = msg["meta"]["guided"]
    assert guided["kickoff"] and guided["phase_count"] >= 2
    assert "Your learning roadmap" in msg["content"] and "Phase 1" in msg["content"]
    assert msg["meta"]["research"]["performed"] and msg["meta"]["research"]["new_sources"] >= 1
    assert not any(c["task"] == "plan_turn" for c in fake_llm.calls)
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    phases = [p for p in rm["phases"] if p["included"]]
    total = len(phases)

    # 2. "next" -> lesson for phase 1, in lesson mode, phase in progress, concepts recorded as explained
    msg = await say(client, worker, wid, sid, "next")
    g = msg["meta"]["guided"]
    assert (g["phase_index"], g["phase_count"]) == (1, total) and g["phase_key"] == phases[0]["key"]
    assert "lesson" in g and isinstance(g["lesson"]["weak_concepts"], list)
    answer_call = [c for c in fake_llm.calls if c["task"] == "answer"][-1]
    assert "LESSON MODE. Teach Phase 1" in answer_call["messages"][-1].content
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    assert rm["phases"][0]["status"] == "in_progress"
    assert all(it["coverage"]["explained"] for it in rm["phases"][0]["items"] if it["included"])

    # a normal question inside a phase is answered normally (not a new lesson)
    msg = await say(client, worker, wid, sid, "How is a qubit different from a classical bit?")
    assert not (msg["meta"].get("guided") or {}).get("lesson")

    # 3. "next" completes phase 1 and teaches phase 2
    msg = await say(client, worker, wid, sid, "next phase")
    assert msg["meta"]["guided"]["phase_index"] == 2
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    assert rm["phases"][0]["status"] == "completed" and rm["phases"][1]["status"] == "in_progress"
    # moving on is progress, not proof of mastery: concepts are not marked completed
    assert not any(it["coverage"]["completed"] for it in rm["phases"][0]["items"])

    # 4. walk to the end; the final "next" finishes the course and offers the teaching pack
    for _ in range(total - 2):
        await say(client, worker, wid, sid, "next")
    msg = await say(client, worker, wid, sid, "next")
    assert msg["meta"]["guided"]["finished"] and "generate teaching pack" in msg["content"]
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    assert all(p["status"] == "completed" for p in rm["phases"] if p["included"])

    # 5. teaching pack: notes + test + answer key + PDFs in one job
    msg = await say(client, worker, wid, sid, "generate teaching pack")
    pack_job = msg["meta"]["guided"]["pack_job_id"]
    await run_jobs(worker)
    job = await job_ok(client, pack_job)
    kinds = {a["kind"]: a["artifact_id"] for a in job["result"]["artifacts"]}
    assert set(kinds) == {"teacher_notes", "student_notes", "question_paper", "answer_key", "student_pack"}
    assert not job["result"]["warnings"]
    paper = await client.get(f"/api/workspaces/{wid}/artifacts/{kinds['question_paper']}/download")
    key = await client.get(f"/api/workspaces/{wid}/artifacts/{kinds['answer_key']}/download")
    paper_text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(paper.content)).pages)
    key_text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(key.content)).pages)
    assert "Answer:" not in paper_text and "Answer:" in key_text


async def test_question_before_roadmap_gets_roadmap_and_answer(client, worker):
    ws = await create_workspace(client, "iot sensors mqtt", level=0)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
    msg = await say(client, worker, wid, sid, "What does a temperature sensor measure?")
    assert "Your learning roadmap" in msg["content"] and "---" in msg["content"]  # roadmap, then the answer
    assert msg["meta"]["guided"]["kickoff"]


async def test_familiar_level_without_roadmap_is_not_forced_into_tutorial(client, worker):
    ws = await create_workspace(client, "cloud computing serverless", level=1)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
    msg = await say(client, worker, wid, sid, "What is AWS Lambda?")
    assert "Your learning roadmap" not in msg["content"]
    assert (await client.get(f"/api/workspaces/{wid}/roadmap")).json() is None


async def test_teaching_pack_api_and_partial_failure(client, worker):
    ws = await create_workspace(client, "a topic nobody researched", level=2)
    r = await client.post(f"/api/workspaces/{ws['id']}/teaching-pack", json={"teacher_name": "Ms. K"})
    assert r.status_code == 202
    await run_jobs(worker)
    job = (await client.get(f"/api/jobs/{r.json()['id']}")).json()
    # with no research the notes cannot be written: the job fails with a clear message instead of empty PDFs
    assert job["status"] == "failed" and "research" in job["error"]["message"].lower()


async def test_interactive_jobs_are_claimed_before_background(container, client):
    ws = await create_workspace(client, "Quantum")
    async with container.db.session() as s:
        bg = await enqueue(s, kind="graph_extract", user_id="teacher-a", workspace_id=ws["id"], payload={})
        chat = await enqueue(s, kind="chat_turn", user_id="teacher-a", workspace_id=ws["id"], payload={})
    async with container.db.session() as s:
        first = await claim_next(s, "w", 60)
    assert first == chat.id  # interactive first even though the background job is older
    async with container.db.session() as s:
        assert await claim_next(s, "w", 60, allow_background=False) is None  # reserved slot: no background
    async with container.db.session() as s:
        assert await claim_next(s, "w", 60) == bg.id


@pytest.mark.nodb
async def test_low_priority_llm_calls_leave_a_reserve():
    bucket = TokenBucket(6000)  # 100 tokens/s
    await bucket.acquire(3000)  # ~3000 left
    t0 = time.monotonic()
    await bucket.acquire(1000)  # interactive: immediate
    assert time.monotonic() - t0 < 0.5
    t0 = time.monotonic()
    low = asyncio.create_task(bucket.acquire(500, low_priority=True))  # needs 500 + 2100 reserve
    await asyncio.sleep(0.2)
    assert not low.done()
    await asyncio.wait_for(low, timeout=10)
    assert time.monotonic() - t0 > 0.3


@pytest.mark.nodb
def test_concept_coverage_terms():
    from app.services.tutor.guide import concept_terms, term_overlap

    assert concept_terms("Scoped atomics in Rust") == ["scop", "atomic", "rust"]
    assert term_overlap(concept_terms("Rust ownership and borrowing"),
                        "Ownership rules: Rust borrows are checked at compile time") == 1.0
    # same domain but not about the concept -> not counted as covered
    assert term_overlap(concept_terms("Rust ownership and borrowing"),
                        "Please see CONTRIBUTING.md if you want to contribute to this Rust project") < 0.6


async def test_lesson_researches_only_uncovered_concepts(client, worker, fake_llm):
    ws = await create_workspace(client, "quantum computing qubits", level=0)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
    await say(client, worker, wid, sid, "start")
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    phases = [{**p, "items": [dict(i) for i in p["items"]]} for p in rm["phases"]]
    covered = phases[0]["items"][0]["title"]
    # the teacher adds a concept the kickoff research never touched
    phases[0]["items"].append({"title": "Zigbee mesh networking", "description": ""})
    r = await client.put(f"/api/workspaces/{wid}/roadmap", json={"expected_version": rm["version"], "phases": phases})
    assert r.status_code == 200, r.text
    msg = await say(client, worker, wid, sid, "next")
    lesson = msg["meta"]["guided"]["lesson"]
    assert "Zigbee mesh networking" in lesson["weak_concepts"] and covered not in lesson["weak_concepts"]
    assert lesson["research_queries"] and lesson["new_sources"] >= 1
