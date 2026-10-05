"""End-to-end demonstration: topic -> research -> roadmap -> notes -> assessment -> answer key -> PDFs.

Drives the real HTTP API (in-process ASGI) and the real job worker against the configured
PostgreSQL / Qdrant.

  Offline (deterministic FakeLLM + fixture search, no API keys):
      uv run python scripts/demo_workflow.py --topic "quantum computing qubits" --level 1
  Live (Groq + Tavily from .env):
      uv run python scripts/demo_workflow.py --live --topic "Photosynthesis" --level 0

PDFs are written to ./demo_output/<workspace-id>/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.container import Container  # noqa: E402
from app.core.config import BACKEND_DIR, Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker.handlers import HANDLERS  # noqa: E402
from app.worker.runner import Worker  # noqa: E402


def step(msg: str) -> None:
    print(f"\n=== {msg}")


async def run(args: argparse.Namespace) -> None:
    overrides = {} if args.live else {
        "env": "development", "llm_provider": "fake", "search_provider": "fixture", "qdrant_url": "",
        "qdrant_path": ":memory:", "search_fixture_dir": BACKEND_DIR / "tests" / "fixtures" / "search"}
    settings = Settings(**overrides)
    c = Container(settings)
    await c.vectors.ensure_collection()
    worker = Worker(c, HANDLERS, concurrency=1, worker_id="demo")
    app = create_app(settings, container=c)
    app.state.container = c
    out_root = Path(args.out)
    headers = {"X-User-Id": args.user}
    t0 = time.time()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo",
                                 headers=headers, timeout=None) as http:

        async def wait(job: dict) -> dict:
            await worker.run_until_idle()
            j = (await http.get(f"/api/jobs/{job['id']}")).json()
            steps = " > ".join(s["step"] for s in j["progress"].get("steps", []))
            print(f"    job {j['kind']}: {j['status']} ({steps})")
            if j["status"] != "succeeded":
                raise SystemExit(f"job failed: {json.dumps(j['error'])[:800]}")
            return j

        step(f"1. Create workspace '{args.topic}' (familiarity level {args.level})")
        ws = (await http.post("/api/workspaces", json={
            "topic": args.topic, "familiarity_level": args.level, "audience": "secondary school students",
            "academic_level": "grade 11", "teaching_goals": ["explain the core ideas", "apply them to examples"],
            "time_available_minutes": 180})).json()
        wid = ws["id"]
        sid = (await http.get(f"/api/workspaces/{wid}/overview")).json()["latest_session_id"]
        print(f"    workspace {wid}")

        step("2. Research through the chat agent")
        turn = (await http.post(f"/api/workspaces/{wid}/sessions/{sid}/messages",
                                json={"content": f"Please research {args.topic} for my lessons"})).json()
        await wait(turn["job"])
        reply = (await http.get(f"/api/workspaces/{wid}/sessions/{sid}/messages")).json()[-1]
        print(f"    assistant: {reply['content'][:300]!r}")
        print(f"    citations: {[c['title'] for c in reply['citations']]}")
        step("3. Generate roadmap")
        await wait((await http.post(f"/api/workspaces/{wid}/roadmap/generate", json={})).json())
        rm = (await http.get(f"/api/workspaces/{wid}/roadmap")).json()
        for i, p in enumerate(rm["phases"], 1):
            print(f"    Phase {i}: {p['title']} -> {[it['title'] for it in p['items']]}")
        if args.level == 0:
            step("3b. Level 0: learn phase 1, then mark it complete")
            r = (await http.post(f"/api/workspaces/{wid}/roadmap/phases/{rm['phases'][0]['key']}/start",
                                 json={"session_id": sid})).json()
            await wait(r["job"])
            r = (await http.post(f"/api/workspaces/{wid}/sessions/{sid}/messages",
                                 json={"content": "I finished this phase"})).json()
            await wait(r["job"])

        gaps = (await http.get(f"/api/workspaces/{wid}/gaps")).json()
        print(f"    gap counts: {gaps['counts']}")

        step("4. Generate teacher and student notes")
        for kind in ("teacher", "student"):
            await wait((await http.post(f"/api/workspaces/{wid}/notes/generate", json={"kind": kind})).json())

        step("5. Configure and generate an assessment (strict coverage)")
        cfg = {"title": f"{args.topic.title()} - Unit Test", "duration_minutes": 40, "coverage_mode": "strict",
               "difficulty": "mixed", "total_marks": 20, "student_level": "grade 11",
               "sections": [{"question_type": "mcq", "count": 4, "marks_each": 1},
                            {"question_type": "true_false", "count": 2, "marks_each": 1},
                            {"question_type": "short_answer", "count": 2, "marks_each": 3},
                            {"question_type": "long_answer", "count": 1, "marks_each": 8}]}
        r = await http.post(f"/api/workspaces/{wid}/assessments", json=cfg)
        if r.status_code != 202:
            raise SystemExit(r.text)
        await wait(r.json()["job"])
        aid = r.json()["assessment"]["id"]
        a = (await http.get(f"/api/workspaces/{wid}/assessments/{aid}")).json()
        rep = a["validation_report"]
        print(f"    {rep['question_count']} questions, {rep['total_marks']} marks, consistent={rep['consistent']}, "
              f"flagged={rep['flagged_questions']}")

        step("6. Answer key + PDFs")
        out = out_root / wid
        out.mkdir(parents=True, exist_ok=True)
        meta = {"teacher_name": "Demo Teacher", "institution": "Demo School", "class_name": "Grade 11"}
        for kind in ("teacher_notes", "student_notes", "question_paper", "answer_key", "student_pack"):
            body = {"kind": kind, **meta}
            if kind in ("question_paper", "answer_key", "student_pack"):
                body["assessment_id"] = aid
            j = await wait((await http.post(f"/api/workspaces/{wid}/exports", json=body)).json())
            art_id = j["result"]["artifact_id"]
            pdf = await http.get(f"/api/workspaces/{wid}/artifacts/{art_id}/download")
            path = out / f"{kind}.pdf"
            path.write_bytes(pdf.content)
            print(f"    {path} ({len(pdf.content) // 1024} KB)")

    await c.close()
    print(f"\nDone in {time.time() - t0:.0f}s. Mode: {'LIVE (Groq + Tavily)' if args.live else 'offline fixtures + FakeLLM'}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topic", default="quantum computing qubits")
    p.add_argument("--level", type=int, choices=[0, 1, 2], default=1)
    p.add_argument("--live", action="store_true", help="use Groq/Tavily from .env instead of offline fakes")
    p.add_argument("--user", default="demo-teacher")
    p.add_argument("--out", default="demo_output")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
