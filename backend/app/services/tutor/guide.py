"""Guided learning flow: roadmap first, then one structured lesson per phase, then a teaching pack.

- Kickoff: a quick, broad search -> roadmap planned straight from the passages -> the roadmap is shown in
  the chat.
- "next": completes the current phase and teaches the next one as a structured lesson. Research is done
  just in time: only phase concepts that local knowledge does not cover yet trigger focused searches.
- Finished (or on request): one job generates teacher + student notes, a test paper, the answer key
  and all PDFs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.core.concepts import concept_terms
from app.core.logging import get_logger
from app.db.models import RoadmapPhase, RoadmapVersion, Workspace
from app.jobs.queue import enqueue
from app.services.research.retrieval import RetrievedPassage

if TYPE_CHECKING:
    from app.container import Container
    from app.services.research.agent import ResearchOutcome

log = get_logger(__name__)

QuickIntent = Literal["start", "next", "pack"]

_NEXT = re.compile(r"^\s*(ok(ay)?[\s,]+)?(next( phase)?|continue|go on|move on|proceed|next one)\W*$", re.I)
_START = re.compile(r"^\s*(start|begin|let'?s (start|begin|go)|start (the )?(course|learning|roadmap)|"
                    r"start learning|teach me)\W*$", re.I)
_PACK = re.compile(r"\b(generate|create|make|build)\b.{0,40}\b(teaching pack|materials|pack|notes.{0,30}"
                   r"(test|paper|quiz|questions))\b", re.I)


def quick_intent(message: str) -> QuickIntent | None:
    """Deterministic commands of the guided flow (no LLM call needed to understand them)."""
    if _NEXT.match(message):
        return "next"
    if _START.match(message):
        return "start"
    if _PACK.search(message):
        return "pack"
    return None


@dataclass
class LessonMaterial:
    phase: RoadmapPhase
    index: int
    total: int
    passages: list[RetrievedPassage] = field(default_factory=list)
    research_queries: list[str] = field(default_factory=list)
    weak_concepts: list[str] = field(default_factory=list)
    new_source_ids: list[str] = field(default_factory=list)


class GuidedTutor:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.settings = c.settings

    # ------------------------------------------------------------------ kickoff

    @staticmethod
    def kickoff_queries(ws: Workspace) -> list[str]:
        topic = ws.topic.strip()
        audience = f" for {ws.academic_level or ws.audience}" if (ws.academic_level or ws.audience) else ""
        return [f"{topic} overview introduction", f"{topic} key concepts fundamentals{audience}",
                f"{topic} prerequisites what to learn first"]

    async def kickoff(self, ws: Workspace, user_id: str, session_id: str, progress=None
                      ) -> tuple[RoadmapVersion, ResearchOutcome]:
        agent = self.c.research
        outcome = await agent.research(ws.id, self.kickoff_queries(ws), progress, max_new_sources=6)
        if progress:
            await progress("planning your roadmap", 55)
        passages = await self.c.retrieval.search(ws.id, f"{ws.topic} overview key concepts", top_k=10)
        rm = await self.c.roadmap.generate(
            ws.id, passages=passages,
            instructions=("Beginner learning path: start from fundamentals and prerequisites, one coherent idea per "
                          "phase, 4-7 phases, each teachable in a single lesson."))
        return rm, outcome

    @staticmethod
    def roadmap_markdown(ws: Workspace, rm: RoadmapVersion) -> str:
        phases = [p for p in rm.phases if p.included]
        lines = [f"## Your learning roadmap: {ws.topic}", "",
                 f"{rm.summary}".strip() or f"{len(phases)} phases, from fundamentals to applications.", ""]
        for i, p in enumerate(phases, 1):
            concepts = ", ".join(it.title for it in p.items if it.included)
            minutes = f" (~{p.estimated_minutes} min)" if p.estimated_minutes else ""
            lines += [f"**Phase {i}: {p.title}**{minutes}  \n{concepts}", ""]
        lines += ["Say **next** (or press *Next phase*) to start Phase 1. Ask questions at any point."]
        return "\n".join(lines)

    # ------------------------------------------------------------------ lessons

    async def prepare_lesson(self, ws: Workspace, user_id: str, session_id: str, phase: RoadmapPhase,
                             index: int, total: int, progress=None) -> LessonMaterial:
        """Collect passages for every concept of the phase, researching only what is not covered yet."""
        lesson = LessonMaterial(phase=phase, index=index, total=total)
        concepts = [it.title for it in phase.items if it.included]
        relevant = {c: await self.c.retrieval.relevant_for_concept(ws.id, c) for c in concepts}
        lesson.weak_concepts = [c for c in concepts if not relevant[c]]
        if lesson.weak_concepts:
            lesson.research_queries = [self.concept_query(ws, c) for c in lesson.weak_concepts[:3]]
            if progress:
                await progress(f"researching phase {index}", 20, ", ".join(lesson.weak_concepts[:3]))
            outcome = await self.c.research.research(ws.id, lesson.research_queries, progress, max_new_sources=4)
            lesson.new_source_ids = outcome.new_source_ids
            for c in lesson.weak_concepts:
                relevant[c] = await self.c.retrieval.relevant_for_concept(ws.id, c)
        seen: set[str] = set()
        for concept in concepts:  # up to 2 passages per concept, in teaching order
            for p in relevant[concept][:2]:
                if p.chunk_id not in seen:
                    seen.add(p.chunk_id)
                    lesson.passages.append(p)
        if len(lesson.passages) < 3:  # thin coverage: add the best topic-level passages as background
            for p in await self.c.retrieval.search(ws.id, f"{phase.title} {ws.topic}", top_k=4):
                if p.chunk_id not in seen:
                    seen.add(p.chunk_id)
                    lesson.passages.append(p)
        lesson.passages = lesson.passages[:12]
        return lesson

    @staticmethod
    def concept_query(ws: Workspace, concept: str) -> str:
        """Focused web query: the concept itself, anchored to the subject only when the name is ambiguous."""
        short_topic = re.split(r"[:(\-–]", ws.topic, maxsplit=1)[0].strip()
        if len(concept_terms(concept)) >= 3 or short_topic.lower() in concept.lower():
            return f"{concept} explained"
        return f"{concept} in {short_topic}"

    @staticmethod
    def lesson_instructions(lesson: LessonMaterial) -> str:
        ph = lesson.phase
        items = [it for it in ph.items if it.included]
        concept_lines = "\n".join(
            f"- {it.title}" + (f" (objectives: {'; '.join(it.objectives)})" if it.objectives else "")
            + (f" (examples to use: {'; '.join(it.examples)})" if it.examples else "") for it in items)
        return (
            f"LESSON MODE. Teach Phase {lesson.index} of {lesson.total}: \"{ph.title}\".\n"
            f"Phase objectives: {'; '.join(ph.objectives) or '-'}\n"
            f"Concepts, in teaching order:\n{concept_lines}\n\n"
            "Write a complete, self-contained lesson in Markdown with this structure:\n"
            f"## Phase {lesson.index}: {ph.title}\n"
            "**What you will learn**: 2-4 bullet objectives.\n"
            "Then one `###` section per concept, in order: a plain-language explanation from first principles, "
            "a concrete example or analogy, and how it connects to the previous concept. Cite source passages.\n"
            "### Common misconceptions: 2-3 bullets.\n"
            "### Check your understanding: 2-3 short questions (do NOT give the answers; the teacher replies "
            "and you will check them).\n"
            "End with one line saying what the next phase covers, or that this was the final phase.\n"
            "Set concepts_explained to the concepts you actually taught."
        )

    # ------------------------------------------------------------------ teaching pack

    async def enqueue_pack(self, ws: Workspace, user_id: str, request: dict | None = None) -> str:
        async with self.c.db.session() as s:
            job = await enqueue(s, kind="generate_pack", user_id=user_id, workspace_id=ws.id,
                                payload={"request": request or {}}, max_attempts=2)
            return job.id

    @staticmethod
    def finished_markdown(ws: Workspace, total: int) -> str:
        return (f"## You have completed all {total} phases of *{ws.topic}*\n\n"
                "Next step: say **generate teaching pack** (or press the button) to create teacher notes, "
                "student notes, a question paper and a separate answer key as PDFs, built from what you "
                "covered. You can still revisit any phase (e.g. *go to phase 2*) or ask follow-up questions.")
