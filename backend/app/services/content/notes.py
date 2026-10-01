"""Teacher and student notes generated from accumulated research.

Material for each section comes from: roadmap phase items (or a graph-derived
outline when there is no roadmap), knowledge-graph claims for those concepts
(with status/perspectives), evidence passages behind the claims, semantic
retrieval, and coverage records (what was explained/discussed). Citations are
validated against the passages actually provided and mapped to a global
reference list of real sources.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.core.errors import NotFoundError, ValidationFailed
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.models import NotesDocument, ResearchSession, Source, Workspace
from app.llm.structured import UNTRUSTED_POLICY, generate_structured, untrusted
from app.services.knowledge.service import workspace_lock

if TYPE_CHECKING:
    from app.container import Container

log = get_logger(__name__)
_MARKER_RE = re.compile(r"\[(S\d{1,3})\]")


class NotesConfig(BaseModel):
    kind: Literal["teacher", "student"] = "teacher"
    title: str | None = Field(default=None, max_length=300)
    phase_keys: list[str] = Field(default_factory=list)  # empty = all included phases
    concept_keys: list[str] = Field(default_factory=list)  # optional finer selection
    depth: Literal["brief", "standard", "detailed"] = "standard"
    audience_override: str | None = Field(default=None, max_length=300)
    include_exercises: bool = True
    include_misconceptions: bool = True
    include_discussion_prompts: bool = True
    extra_instructions: str = Field(default="", max_length=2000)


class OutlineSection(BaseModel):
    title: str = Field(max_length=200)
    concepts: list[str] = Field(min_length=1, max_length=10)


class NotesOutline(BaseModel):
    sections: list[OutlineSection] = Field(min_length=1, max_length=12)


class SectionOut(BaseModel):
    heading: str = Field(max_length=200)
    body_markdown: str = Field(min_length=1, max_length=20000)
    key_takeaways: list[str] = Field(default_factory=list, max_length=10)
    misconceptions: list[str] = Field(default_factory=list, max_length=8)
    teaching_strategies: list[str] = Field(default_factory=list, max_length=8)
    discussion_prompts: list[str] = Field(default_factory=list, max_length=8)
    practice_exercises: list[str] = Field(default_factory=list, max_length=10)
    cited_sources: list[str] = Field(default_factory=list)
    concepts_covered: list[str] = Field(default_factory=list, max_length=15)


def _system(kind: str, depth: str, audience: str, language: str) -> str:
    common = (f"Write in language: {language}. Use Markdown with headings (###), lists and tables where useful. "
              "Use $...$ for inline and $$...$$ for display math, fenced code blocks for code. "
              "Base factual content on the provided passages and graph facts and cite them inline with their "
              "markers like [S2]. Only cite provided markers; never invent sources, URLs or quotations. If you add "
              "general background not supported by the passages, keep it uncontroversial and label it "
              "'(general background)'. Where sources disagree or a claim is contested, present the perspectives "
              f"with attribution. Depth: {depth}.\n{UNTRUSTED_POLICY}")
    if kind == "teacher":
        return ("You write detailed TEACHER NOTES for one section of a course. Include clear definitions, "
                "thorough explanations, worked examples or worked problems, analogies, comparisons, common "
                "student misconceptions, teaching strategies and optional discussion prompts. The teacher will "
                f"teach students who are: {audience}.\n" + common)
    return ("You write STUDENT NOTES for one section of a course. Be clear and friendly; define terms, give "
            "relevant examples, short summaries and practice questions. Avoid teacher-only commentary. "
            f"Students: {audience}. Match vocabulary and depth to them.\n" + common)


class NotesService:
    def __init__(self, c: "Container") -> None:
        self.c = c

    async def _outline(self, ws: Workspace, cfg: NotesConfig) -> list[dict[str, Any]]:
        """[{title, concepts:[{key,name,objectives,examples}]}] from roadmap, or a graph-derived outline."""
        rm = await self.c.roadmap.current(ws.id)
        if rm is not None:
            out = []
            for ph in rm.phases:
                if not ph.included or (cfg.phase_keys and ph.key not in cfg.phase_keys):
                    continue
                concepts = [{"key": i.concept_key, "name": i.title, "objectives": i.objectives,
                             "examples": i.examples} for i in ph.items
                            if i.included and (not cfg.concept_keys or i.concept_key in cfg.concept_keys)]
                if concepts:
                    out.append({"title": ph.title, "objectives": ph.objectives, "concepts": concepts})
            if out:
                return out
        outline = await self.c.graph.concept_outline(ws.id, limit=60)
        if not outline["nodes"]:
            raise ValidationFailed("No research or roadmap yet: research the topic before generating notes")
        names = [n["name"] for n in outline["nodes"]]
        draft = await generate_structured(
            self.c.llm, task="notes_outline", tier="fast", schema=NotesOutline, max_tokens=1500,
            system="Group researched concepts into a logical sequence of teaching sections (3-8). Use only the "
                   "given concept names.",
            user=f"Topic: {ws.topic}\nAudience: {ws.audience}\nConcepts:\n" + "\n".join(f"- {n}" for n in names))
        index = await self.c.roadmap.concept_index(ws.id)
        allowed = {n.lower() for n in names}
        sections = []
        for sec in draft.sections:
            cs = [{"key": self.c.roadmap.concept_key(ws.id, n, index), "name": n, "objectives": [], "examples": []}
                  for n in sec.concepts if n.lower() in allowed]
            if cs:
                sections.append({"title": sec.title, "objectives": [], "concepts": cs})
        if not sections:
            raise ValidationFailed("Could not derive a notes outline from the research")
        return sections

    async def _material(self, ws_id: str, section: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        keys = [c["key"] for c in section["concepts"]]
        facts = await self.c.graph.facts_for_entities(ws_id, keys, limit=40)
        chunk_ids = await self.c.graph.evidence_chunks_for_entities(ws_id, keys, limit=12)
        passages = await self.c.retrieval.chunks_by_ids(ws_id, chunk_ids)
        query = section["title"] + ": " + ", ".join(c["name"] for c in section["concepts"])
        extra = await self.c.retrieval.search(ws_id, query, top_k=6)
        seen = {p.chunk_id for p in passages}
        passages += [p for p in extra if p.chunk_id not in seen]
        return facts, [{"chunk_id": p.chunk_id, "source_id": p.source_id, "title": p.title, "url": p.url,
                        "text": p.text} for p in passages[:12]]

    async def generate(self, workspace_id: str, cfg: NotesConfig, progress=None) -> NotesDocument:
        async with self.c.db.session() as s:
            ws = await s.get(Workspace, workspace_id)
            sessions = (await s.execute(select(ResearchSession).where(
                ResearchSession.workspace_id == workspace_id))).scalars().all()
        if ws is None:
            raise NotFoundError("Workspace not found")
        coverage = await self.c.coverage.records(workspace_id)
        outline = await self._outline(ws, cfg)
        audience = cfg.audience_override or f"{ws.audience or 'students'} ({ws.academic_level or 'general level'})"
        system = _system(cfg.kind, cfg.depth, audience, ws.language or "en")
        convo = "\n".join(f"- {sx.rolling_summary[:1200]}" for sx in sessions if sx.rolling_summary)

        ref_numbers: dict[str, int] = {}
        sections_out: list[dict[str, Any]] = []
        all_concepts: list[str] = []
        for si, sec in enumerate(outline):
            if progress:
                await progress(f"writing section {si + 1}/{len(outline)}", 10 + int(80 * si / max(1, len(outline))),
                               sec["title"])
            facts, passages = await self._material(workspace_id, sec)
            markers = {f"S{i}": p for i, p in enumerate(passages, start=1)}
            cov_lines = []
            for cpt in sec["concepts"]:
                r = coverage.get(cpt["key"])
                stages = [st for st in ("researched", "explained", "discussed", "completed")
                          if r is not None and getattr(r, f"{st}_at")]
                fb = f"; teacher self-reported: {r.self_reported_understanding}" if r and r.self_reported_understanding else ""
                cov_lines.append(f"- {cpt['name']}: {', '.join(stages) or 'planned only'}{fb}")
            user = (
                f"Course topic: {ws.topic}\nSection {si + 1} of {len(outline)}: {sec['title']}\n"
                f"Section objectives: {'; '.join(sec.get('objectives') or []) or '-'}\n"
                f"Concepts in this section (in order):\n" + "\n".join(
                    f"- {c['name']}" + (f" | objectives: {'; '.join(c['objectives'])}" if c['objectives'] else "")
                    + (f" | examples to use: {'; '.join(c['examples'])}" if c['examples'] else "")
                    for c in sec["concepts"]) +
                f"\n\nCoverage so far (what the teacher researched/discussed):\n{chr(10).join(cov_lines)}\n"
                f"Conversation summaries:\n{convo or '-'}\n\n"
                "Knowledge-graph facts:\n" + "\n".join(
                    f"- {f['text']} [status: {f['status']}]" + (f" [perspective: {'; '.join(f['perspectives'])}]"
                                                                if f.get('perspectives') else "") for f in facts) +
                "\n\nSource passages:\n" + "\n\n".join(
                    untrusted("source", p["text"], id=m, title=p["title"][:120]) for m, p in markers.items()) +
                f"\n\nOptions: exercises={cfg.include_exercises}, misconceptions={cfg.include_misconceptions}, "
                f"discussion_prompts={cfg.include_discussion_prompts}.\n"
                f"Extra teacher instructions: {cfg.extra_instructions or '-'}"
            )
            out = await generate_structured(self.c.llm, task=f"notes_section_{cfg.kind}", system=system, user=user,
                                            schema=SectionOut, tier="smart", max_tokens=4000)
            md, used_sources = self._compose(out, cfg, markers, ref_numbers)
            covered_keys = [c["key"] for c in sec["concepts"]]
            all_concepts += covered_keys
            sections_out.append({"id": new_id(), "heading": out.heading or sec["title"], "markdown": md,
                                 "concept_keys": covered_keys, "source_ids": used_sources})

        # references section from real source rows only
        source_ids = list(ref_numbers)
        async with self.c.db.session() as s:
            rows = (await s.execute(select(Source).where(Source.workspace_id == workspace_id,
                                                         Source.id.in_(source_ids)))).scalars().all()
        by_id = {r.id: r for r in rows}
        refs = []
        for sid, n in sorted(ref_numbers.items(), key=lambda kv: kv[1]):
            r = by_id.get(sid)
            if r is None:
                continue
            line = f"{n}. {r.title or 'Untitled source'}"
            if r.site_name:
                line += f". {r.site_name}"
            if r.published_at:
                line += f" ({r.published_at})"
            if r.url:
                line += f". <{r.url}>"
            line += f". Retrieved {r.retrieved_at.date().isoformat()}."
            refs.append(line)
        if refs:
            sections_out.append({"id": new_id(), "heading": "References", "markdown": "\n".join(refs),
                                 "concept_keys": [], "source_ids": source_ids})

        title = cfg.title or f"{ws.topic}: {'Teacher' if cfg.kind == 'teacher' else 'Student'} Notes"
        doc = await self._save(workspace_id, cfg.kind, title, sections_out, cfg.model_dump(),
                               list(dict.fromkeys(all_concepts)), source_ids, edited=False)
        await self.c.graph.link_teaching_material(workspace_id, doc.id, title, f"{cfg.kind}_notes",
                                                  list(dict.fromkeys(all_concepts)))
        return doc

    @staticmethod
    def _compose(out: SectionOut, cfg: NotesConfig, markers: dict[str, dict],
                 ref_numbers: dict[str, int]) -> tuple[str, list[str]]:
        used: list[str] = []

        def sub(m: re.Match) -> str:
            p = markers.get(m.group(1))
            if p is None:
                return ""
            sid = p["source_id"]
            if sid not in ref_numbers:
                ref_numbers[sid] = len(ref_numbers) + 1
            used.append(sid)
            return f"[{ref_numbers[sid]}]"

        parts = [_MARKER_RE.sub(sub, out.body_markdown.strip())]

        def bullets(title: str, items: list[str]) -> None:
            if items:
                parts.append(f"#### {title}\n" + "\n".join(f"- {_MARKER_RE.sub(sub, i)}" for i in items))

        bullets("Key takeaways", out.key_takeaways)
        if cfg.kind == "teacher":
            if cfg.include_misconceptions:
                bullets("Common misconceptions", out.misconceptions)
            bullets("Teaching strategies", out.teaching_strategies)
            if cfg.include_discussion_prompts:
                bullets("Discussion prompts", out.discussion_prompts)
        if cfg.include_exercises and out.practice_exercises:
            parts.append("#### Practice\n" + "\n".join(f"{i}. {_MARKER_RE.sub(sub, e)}"
                                                       for i, e in enumerate(out.practice_exercises, 1)))
        return "\n\n".join(parts), list(dict.fromkeys(used))

    async def _save(self, workspace_id: str, kind: str, title: str, sections: list[dict], config: dict,
                    concept_keys: list[str], source_ids: list[str], *, edited: bool,
                    based_on: NotesDocument | None = None) -> NotesDocument:
        async with self.c.db.session() as s:
            await workspace_lock(s, workspace_id, f"notes:{kind}")
            last = (await s.execute(select(NotesDocument.version).where(
                NotesDocument.workspace_id == workspace_id, NotesDocument.kind == kind)
                .order_by(NotesDocument.version.desc()).limit(1))).scalar_one_or_none() or 0
            await s.execute(update(NotesDocument).where(NotesDocument.workspace_id == workspace_id,
                                                        NotesDocument.kind == kind).values(is_current=False))
            doc = NotesDocument(workspace_id=workspace_id, kind=kind, title=title[:300], version=last + 1,
                                is_current=True, sections=sections, config=config, concept_keys=concept_keys,
                                source_ids=source_ids, edited=edited)
            s.add(doc)
            await s.flush()
            return doc

    async def save_edit(self, workspace_id: str, doc_id: str, title: str, sections: list[dict]) -> NotesDocument:
        async with self.c.db.session() as s:
            doc = await s.get(NotesDocument, doc_id)
        if doc is None or doc.workspace_id != workspace_id:
            raise NotFoundError("Notes not found")
        prior = {sec["id"]: sec for sec in doc.sections}
        clean = []
        for sec in sections:
            if not (sec.get("heading") or sec.get("markdown")):
                continue
            base = prior.get(sec.get("id") or "", {})
            clean.append({"id": sec.get("id") or new_id(), "heading": (sec.get("heading") or "")[:300],
                          "markdown": (sec.get("markdown") or "")[:60000],
                          "concept_keys": base.get("concept_keys", []), "source_ids": base.get("source_ids", [])})
        if not clean:
            raise ValidationFailed("Notes must have at least one section")
        new_doc = await self._save(workspace_id, doc.kind, title or doc.title, clean, doc.config, doc.concept_keys,
                                   doc.source_ids, edited=True)
        await self.c.graph.link_teaching_material(workspace_id, new_doc.id, new_doc.title, f"{doc.kind}_notes",
                                                  doc.concept_keys)
        return new_doc
