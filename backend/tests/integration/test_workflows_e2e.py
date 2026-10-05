"""End-to-end workflows through the HTTP API + worker, one per familiarity level, plus
roadmap editing, coverage/gaps, notes, assessments (strict vs expanded), answer-key separation and PDFs."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from tests.conftest import create_workspace, job_ok, run_jobs

pytestmark = pytest.mark.integration


async def chat(client, worker, wid, sid, text) -> dict:
    r = await client.post(f"/api/workspaces/{wid}/sessions/{sid}/messages", json={"content": text})
    assert r.status_code == 202, r.text
    await run_jobs(worker)
    job = await job_ok(client, r.json()["job"]["id"])
    msgs = (await client.get(f"/api/workspaces/{wid}/sessions/{sid}/messages?limit=1")).json()
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["id"] == job["result"]["assistant_message_id"]
    return msgs[-1]


async def job(client, worker, resp) -> dict:
    assert resp.status_code == 202, resp.text
    body = resp.json()
    jid = body["id"] if "id" in body and "kind" in body else body["job"]["id"]
    await run_jobs(worker)
    return await job_ok(client, jid)


async def pdf_text(client, wid, artifact_id) -> str:
    r = await client.get(f"/api/workspaces/{wid}/artifacts/{artifact_id}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)


async def export(client, worker, wid, **body) -> str:
    j = await job(client, worker, await client.post(f"/api/workspaces/{wid}/exports", json=body))
    return j["result"]["artifact_id"]


# ------------------------------------------------------------------ Level 0: learn first, phase by phase


async def test_level0_beginner_tutorial_flow(client, worker, fake_llm):
    ws = await create_workspace(client, "Quantum computing", level=0, time_available_minutes=240)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]

    # research via chat: the agent decides external research is needed (nothing known yet)
    msg = await chat(client, worker, wid, sid, "Please research qubits and the Bloch sphere for me")
    assert msg["meta"]["research"]["performed"] and msg["meta"]["research"]["new_sources"] == 2
    # beginner without a roadmap: the roadmap comes first, then the answer to the question
    assert msg["meta"]["guided"]["kickoff"] and "Your learning roadmap" in msg["content"]
    assert msg["citations"] and all(c["source_id"] and c["chunk_id"] for c in msg["citations"])
    cov = (await client.get(f"/api/workspaces/{wid}/coverage")).json()
    assert any(c["researched_at"] for c in cov)  # roadmap concepts the sources discuss are marked researched

    # explicit regeneration (v1 came from the guided kickoff)
    rm_job = await job(client, worker, await client.post(f"/api/workspaces/{wid}/roadmap/generate", json={}))
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    assert rm["version"] == rm_job["result"]["version"] == 2 and len(rm["phases"]) >= 2  # v1 came from kickoff
    assert all(item["coverage"]["planned"] for p in rm["phases"] for item in p["items"])

    # start phase 1 (tutorial), ask a follow-up, then explicitly complete it and move on
    p1, p2 = rm["phases"][0], rm["phases"][1]
    r = await client.post(f"/api/workspaces/{wid}/roadmap/phases/{p1['key']}/start", json={"session_id": sid})
    await job(client, worker, r)
    state = (await client.get(f"/api/workspaces/{wid}/sessions/{sid}")).json()["state"]
    assert state["current_phase_id"] == p1["key"]
    await chat(client, worker, wid, sid, "I'm confused about Superposition, can you explain Superposition again?")
    await chat(client, worker, wid, sid, "I finished this phase")
    state = (await client.get(f"/api/workspaces/{wid}/sessions/{sid}")).json()["state"]
    assert p1["key"] in state["completed_phase_ids"] and state["current_phase_id"] == p2["key"]
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    assert rm["phases"][0]["status"] == "completed" and rm["phases"][1]["status"] == "in_progress"
    assert all(i["coverage"]["completed"] for i in rm["phases"][0]["items"])
    assert not any(i["coverage"]["completed"] for i in rm["phases"][1]["items"])  # never inferred

    # skip ahead and revisit
    await client.post(f"/api/workspaces/{wid}/roadmap/navigate",
                      json={"session_id": sid, "action": "skip_phase"})
    nav = (await client.post(f"/api/workspaces/{wid}/roadmap/navigate",
                             json={"session_id": sid, "action": "revisit_phase", "phase_ref": "1"})).json()
    assert nav["current_phase_key"] == p1["key"]

    # coverage distinguishes self-reported understanding from completion
    cov = {c["concept_name"].lower(): c for c in (await client.get(f"/api/workspaces/{wid}/coverage")).json()}
    sup = cov.get("superposition")
    assert sup and sup["self_reported_understanding"] == "confused" and sup["discussed_at"]

    # at the end: notes + test from what was covered
    notes_job = await job(client, worker, await client.post(f"/api/workspaces/{wid}/notes/generate",
                                                           json={"kind": "student"}))
    assert notes_job["result"]["sections"] >= 2
    cfg = {"title": "Quantum basics quiz", "question_types": ["mcq", "true_false"], "num_questions": 4,
           "coverage_mode": "strict", "duration_minutes": 20}
    r = await client.post(f"/api/workspaces/{wid}/assessments", json=cfg)
    await job(client, worker, r)
    a = (await client.get(f"/api/workspaces/{wid}/assessments/{r.json()['assessment']['id']}")).json()
    assert a["status"] == "ready" and len(a["questions"]) == 4


# ------------------------------------------------------------------ Level 1: structured prep


async def test_level1_familiar_flow_with_roadmap_edits_gaps_and_pdfs(client, worker):
    ws = await create_workspace(client, "IoT sensor networks", level=1)
    wid = ws["id"]
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/research",
                                                json={"queries": ["iot sensors mqtt edge"]}))
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/roadmap/generate", json={}))
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()

    # teacher edits: rename + exclude an item, reorder phases, add own item
    phases = [{**p, "items": [dict(i) for i in p["items"]]} for p in rm["phases"]]
    phases[0]["title"] = "Sensors first (my wording)"
    phases[0]["items"][0]["included"] = False
    excluded_key = phases[0]["items"][0]["concept_key"]
    phases[-1]["items"].append({"title": "Classroom lab: build a sensor node", "description": "hands-on"})
    phases = [phases[-1], *phases[:-1]]
    r = await client.put(f"/api/workspaces/{wid}/roadmap", json={"expected_version": rm["version"],
                                                                 "phases": phases})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["version"] == 2 and edited["phases"][0]["teacher_edited"]
    # stale edit is rejected (optimistic concurrency)
    r = await client.put(f"/api/workspaces/{wid}/roadmap", json={"expected_version": 1, "phases": phases})
    assert r.status_code == 422

    # more research arrives, roadmap is regenerated: teacher edits survive
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/research",
                                                json={"queries": ["iot internet of things gateway zigbee"]}))
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/roadmap/generate", json={}))
    regen = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    assert regen["version"] == 3
    titles = [p["title"] for p in regen["phases"]]
    assert "Sensors first (my wording)" in titles
    items = {i["concept_key"]: i for p in regen["phases"] for i in p["items"]}
    assert items[excluded_key]["included"] is False
    assert any(i["title"] == "Classroom lab: build a sensor node" for i in items.values())
    versions = (await client.get(f"/api/workspaces/{wid}/roadmap/versions")).json()
    assert [v["version"] for v in versions] == [3, 2, 1]

    # regenerate one phase only
    target = regen["phases"][-1]
    await job(client, worker, await client.post(
        f"/api/workspaces/{wid}/roadmap/phases/{target['key']}/regenerate", json={"instructions": "simpler"}))
    single = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    assert len(single["phases"]) == len(regen["phases"])
    assert single["phases"][0]["title"] == regen["phases"][0]["title"]

    # gaps: factual, categorized, distinguishes required vs optional
    gaps = (await client.get(f"/api/workspaces/{wid}/gaps")).json()
    assert gaps["counts"]["planned_items"] >= 3 and "not a measure of mastery" in gaps["note"]
    types = {g["type"] for g in gaps["gaps"]}
    assert "missing_teaching_material" in types and "planned_not_covered" in types
    assert {g["kind"] for g in gaps["gaps"]} <= {"required", "recommended", "optional", "info"}

    # teacher + student notes, edit, PDFs
    tn = await job(client, worker, await client.post(f"/api/workspaces/{wid}/notes/generate",
                                                     json={"kind": "teacher"}))
    note = (await client.get(f"/api/workspaces/{wid}/notes/{tn['result']['notes_id']}")).json()
    assert any(s["heading"] == "References" for s in note["sections"])
    refs = next(s for s in note["sections"] if s["heading"] == "References")["markdown"]
    assert "https://example.edu/iot/architecture" in refs or "https://example.net/edge-computing" in refs
    body = note["sections"][0]["markdown"]
    assert "Common misconceptions" in body and "[1]" in body
    sections = [{"id": s["id"], "heading": s["heading"], "markdown": s["markdown"]} for s in note["sections"]]
    sections[0]["markdown"] += "\n\nTeacher's own addition: bring a real thermostat."
    r = await client.put(f"/api/workspaces/{wid}/notes/{note['id']}", json={"title": "My IoT notes",
                                                                         "sections": sections})
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["edited"]
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/notes/generate", json={"kind": "student"}))
    art = await export(client, worker, wid, kind="teacher_notes", teacher_name="Mr. Okafor", institution="Hill HS")
    text = await pdf_text(client, wid, art)
    assert "bring a real thermostat" in text and "Mr. Okafor" in text and "Page 1 of" in text
    art_s = await export(client, worker, wid, kind="student_notes")
    assert "Student notes" in await pdf_text(client, wid, art_s)


# ------------------------------------------------------------------ Level 2: materials directly


async def test_level2_advanced_flow_assessment_and_answer_key_separation(client, worker, fake_llm):
    ws = await create_workspace(client, "Cloud computing", level=2)
    wid = ws["id"]
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/research",
                                                json={"queries": ["cloud computing serverless lambda"]}))
    # no roadmap: notes come from an outline planned from the research
    assert (await client.get(f"/api/workspaces/{wid}/roadmap")).json() is None
    notes = await job(client, worker, await client.post(f"/api/workspaces/{wid}/notes/generate",
                                                        json={"kind": "teacher"}))
    assert any(c["task"] == "roadmap" for c in fake_llm.calls)  # outline planned from passages
    assert (await client.get(f"/api/workspaces/{wid}/roadmap")).json() is None  # ...but not saved as a roadmap
    assert notes["result"]["sections"] >= 2

    types = (await client.get(f"/api/workspaces/{wid}/assessments/question-types")).json()
    assert "coding" in types["types"] and "numerical" in types["types"]

    # inconsistent configuration is rejected before any generation happens
    bad = {"title": "Bad", "sections": [{"question_type": "mcq", "count": 4, "marks_each": 2}], "total_marks": 10}
    r = await client.post(f"/api/workspaces/{wid}/assessments", json=bad)
    assert r.status_code == 422 and r.json()["error"]["details"]["computed_total"] == 8

    cfg = {
        "title": "Cloud unit test", "student_level": "first-year undergraduates", "difficulty": "mixed",
        "coverage_mode": "strict", "duration_minutes": 45, "total_marks": 28, "include_answers_in_paper": False,
        "sections": [
            {"question_type": "mcq", "count": 4, "marks_each": 1},
            {"question_type": "true_false", "count": 2, "marks_each": 1},
            {"question_type": "short_answer", "count": 2, "marks_each": 4},
            {"question_type": "numerical", "count": 1, "marks_each": 6},
            {"question_type": "coding", "count": 1, "marks_each": 8},
        ],
        "custom_instructions": "Use real-world cloud scenarios.",
    }
    r = await client.post(f"/api/workspaces/{wid}/assessments", json=cfg)
    await job(client, worker, r)
    aid = r.json()["assessment"]["id"]
    a = (await client.get(f"/api/workspaces/{wid}/assessments/{aid}")).json()
    rep = a["validation_report"]
    assert a["status"] == "ready" and len(a["questions"]) == 10
    assert rep["total_marks"] == 28 and rep["consistent"], rep["problems"]
    assert rep["section_totals"] == {"S1": 4, "S2": 2, "S3": 8, "S4": 6, "S5": 8}
    qs = a["questions"]
    mcq = [q for q in qs if q["type"] == "mcq"]
    assert all(len(q["choices"]) == 4 and q["answer"]["choice_label"] in "ABCD" for q in mcq)
    assert all(q["source_refs"] for q in qs)  # strict: grounded in researched passages
    num = next(q for q in qs if q["type"] == "numerical")
    assert num["verification"]["numeric"] == "verified"
    code = next(q for q in qs if q["type"] == "coding")
    assert any(f["code"] == "needs_teacher_review" for f in code["flags"])  # code execution disabled by default
    assert len({q["text"] for q in qs}) == 10

    # teacher editing: fix marks, edit, invalid edit rejected, regenerate, delete -> report updates
    short = next(q for q in qs if q["type"] == "short_answer")
    r = await client.patch(f"/api/workspaces/{wid}/assessments/{aid}/questions/{short['id']}",
                           json={"text": "Explain why serverless platforms bill per invocation.", "marks": 5})
    assert r.status_code == 200 and r.json()["origin"] == "edited"
    assert sum(x["marks"] for x in r.json()["rubric"]) == 5
    r = await client.patch(f"/api/workspaces/{wid}/assessments/{aid}/questions/{mcq[0]['id']}",
                           json={"answer": {"choice_index": 9}})
    assert r.status_code == 422
    rq = await job(client, worker, await client.post(
        f"/api/workspaces/{wid}/assessments/{aid}/questions/{mcq[1]['id']}/regenerate",
        json={"instructions": "make it about storage"}))
    a2 = (await client.get(f"/api/workspaces/{wid}/assessments/{aid}")).json()
    regenerated = next(q for q in a2["questions"] if q["id"] == rq["result"]["question_id"])
    assert regenerated["origin"] == "regenerated" and regenerated["text"] != mcq[1]["text"]
    assert a2["validation_report"]["total_marks"] == 29 and not a2["validation_report"]["consistent"]
    await client.delete(f"/api/workspaces/{wid}/assessments/{aid}/questions/{mcq[2]['id']}")
    a3 = (await client.get(f"/api/workspaces/{wid}/assessments/{aid}")).json()
    assert len(a3["questions"]) == 9

    # PDFs: the student paper never contains answers; the key does
    paper = await pdf_text(client, wid, await export(client, worker, wid, kind="question_paper", assessment_id=aid,
                                                     class_name="CS101", teacher_name="Dr. Lee"))
    key = await pdf_text(client, wid, await export(client, worker, wid, kind="answer_key", assessment_id=aid))
    flat_paper = " ".join(paper.split())
    assert "Cloud unit test" in paper and "Duration: 45 minutes" in flat_paper and "CS101" in paper
    for q in a3["questions"]:
        if q["explanation"]:
            assert q["explanation"][:40] not in flat_paper
    assert "Answer:" not in paper and "Explanation" not in paper and "Marking scheme" not in paper
    assert "Answer:" in key and "Marking scheme" in key and "CONFIDENTIAL" in key
    assert "def add" in key  # reference solution only in the key

    # explicit opt-in includes answers in the paper
    cfg2 = {**cfg, "title": "Self-check sheet", "include_answers_in_paper": True}
    r = await client.post(f"/api/workspaces/{wid}/assessments", json=cfg2)
    await job(client, worker, r)
    opted = await pdf_text(client, wid, await export(client, worker, wid, kind="question_paper",
                                                     assessment_id=r.json()["assessment"]["id"]))
    assert "Answer key" in opted and "Answer:" in opted

    # student pack: student notes + paper, never answers
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/notes/generate", json={"kind": "student"}))
    pack = await pdf_text(client, wid, await export(client, worker, wid, kind="student_pack", assessment_id=aid))
    assert "Practice test" in pack and "Answer:" not in pack

    # exports are idempotent: same content -> same artifact
    a_id1 = await export(client, worker, wid, kind="answer_key", assessment_id=aid)
    a_id2 = await export(client, worker, wid, kind="answer_key", assessment_id=aid)
    assert a_id1 == a_id2


async def test_strict_vs_expanded_scope(client, worker):
    ws = await create_workspace(client, "Quantum computing", level=1)
    wid = ws["id"]
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/research",
                                                json={"queries": ["quantum computing qubits"]}))
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/roadmap/generate", json={}))
    rm = (await client.get(f"/api/workspaces/{wid}/roadmap")).json()
    planned_only = {"concept_key": "00000000-0000-5000-8000-000000000000"}
    topic_keys = [rm["phases"][0]["items"][0]["concept_key"], planned_only["concept_key"]]
    base = {"title": "Scope", "question_types": ["short_answer"], "num_questions": 2, "topics": topic_keys}

    r = await client.post(f"/api/workspaces/{wid}/assessments", json={**base, "coverage_mode": "strict"})
    await job(client, worker, r)
    strict = (await client.get(f"/api/workspaces/{wid}/assessments/{r.json()['assessment']['id']}")).json()
    assert strict["validation_report"]["scope"]["excluded_topics_not_covered"] == [planned_only["concept_key"]]
    assert {q["concept_keys"][0] for q in strict["questions"]} == {topic_keys[0]}

    r = await client.post(f"/api/workspaces/{wid}/assessments",
                          json={**base, "topics": [topic_keys[0]], "coverage_mode": "expanded", "num_questions": 4})
    await job(client, worker, r)
    exp = (await client.get(f"/api/workspaces/{wid}/assessments/{r.json()['assessment']['id']}")).json()
    assert exp["validation_report"]["scope"].get("related_concepts_added", 0) >= 1
    assert exp["validation_report"]["coverage_mode"] == "expanded"


async def test_long_conversation_is_summarized_not_truncated(client, worker, fake_llm):
    ws = await create_workspace(client, "French Revolution", level=0)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
    await chat(client, worker, wid, sid, "Please research the French Revolution and the Enlightenment")
    for i in range(6):
        await chat(client, worker, wid, sid, f"Tell me more about the Bastille, point number {i}. " + "detail " * 40)
    sess = (await client.get(f"/api/workspaces/{wid}/sessions/{sid}")).json()
    assert sess["message_count"] == 14
    assert sess["rolling_summary"] and sess["summarized_upto_seq"] > 0
    assert any(c["task"] == "summarize" for c in fake_llm.calls)
    # all messages remain stored
    msgs = (await client.get(f"/api/workspaces/{wid}/sessions/{sid}/messages?limit=200")).json()
    assert len(msgs) == 14
    # the latest answer prompt contains the summary, not the whole transcript
    last_answer = [c for c in fake_llm.calls if c["task"] == "answer"][-1]
    system = last_answer["messages"][0].content
    assert "Conversation summary" in system
    assert "point number 0" not in last_answer["messages"][-1].content
    meta = msgs[-1]["meta"]["context"]
    assert meta["passages"] >= 1 and meta["tokens"] < 32000
    # repeated identical questions reuse local knowledge rather than searching again
    research_runs = [m["meta"]["research"]["performed"] for m in msgs if m["role"] == "assistant"]
    assert research_runs[0] and not any(research_runs[1:])


async def test_grounding_strips_invented_citations_and_urls(client, worker, fake_llm):
    ws = await create_workspace(client, "Quantum computing", level=1)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
    await chat(client, worker, wid, sid, "Please research qubits")
    fake_llm.script("answer", {"answer_markdown": "Qubits [S1] are cool [S99]【S77】. See https://evil.example/x",
                               "cited_sources": ["S1", "S42"], "concepts_explained": ["Qubit"]})
    msg = await chat(client, worker, wid, sid, "What is a qubit?")
    assert "[S99]" not in msg["content"] and "S77" not in msg["content"] and "evil.example" not in msg["content"]
    assert [c["marker"] for c in msg["citations"]] == ["S1"]


async def test_full_workflow_fits_small_free_tier_request_limit(client, worker, fake_llm):
    """With a 6k tokens-per-request budget (Groq free-tier-like), every LLM call must fit."""
    fake_llm.request_limit = 6000
    ws = await create_workspace(client, "French Revolution history", level=1)
    wid = ws["id"]
    sid = (await client.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
    await chat(client, worker, wid, sid, "Please research the French Revolution and the Enlightenment")
    await run_jobs(worker)
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/research",
                                                json={"queries": ["french revolution bastille terror"]}))
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/roadmap/generate", json={}))
    await job(client, worker, await client.post(f"/api/workspaces/{wid}/notes/generate", json={"kind": "teacher"}))
    r = await client.post(f"/api/workspaces/{wid}/assessments", json={
        "title": "Small budget", "question_types": ["mcq", "short_answer", "long_answer"], "num_questions": 9})
    await job(client, worker, r)
    for i in range(4):
        await chat(client, worker, wid, sid, f"Explain the Reign of Terror in more depth, part {i}. " + "context " * 60)
    assert fake_llm.calls and max(c["requested_tokens"] for c in fake_llm.calls) <= 6000
    tasks = {c["task"] for c in fake_llm.calls}
    assert {"plan_turn", "answer", "roadmap", "questions", "summarize"} <= tasks
