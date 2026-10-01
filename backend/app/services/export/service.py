"""Document export: build provider-neutral DocumentSpecs and render them to PDF artifacts.

Answer-key separation is structural: the student question paper is built from
``StudentQuestionView`` objects, which have no answer/explanation/rubric fields.
Answers are only added to a paper when ``include_answers_in_paper`` is explicitly set.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.errors import NotFoundError, ValidationFailed
from app.core.ids import sha256_text
from app.db.models import Artifact, Assessment, NotesDocument, Question, Workspace
from app.providers.pdf.base import DocumentMeta, DocumentSection, DocumentSpec
from app.services.assessment.schemas import TYPE_LABELS, AssessmentConfig, Blueprint, StudentQuestionView
from app.services.assessment.service import student_view

if TYPE_CHECKING:
    from app.container import Container

ExportKind = Literal["student_notes", "teacher_notes", "question_paper", "answer_key", "student_pack"]


class ExportRequest(BaseModel):
    kind: ExportKind
    notes_id: str | None = None
    assessment_id: str | None = None
    teacher_name: str = Field(default="", max_length=120)
    institution: str = Field(default="", max_length=200)
    class_name: str = Field(default="", max_length=120)
    date: str = Field(default="", max_length=40)


def _fmt_marks(m: float) -> str:
    return f"{m:g} mark" + ("" if m == 1 else "s")


def paper_sections(views: list[StudentQuestionView], bp: Blueprint) -> list[DocumentSection]:
    """Question-paper body built ONLY from answer-free views."""
    sections: list[DocumentSection] = []
    by_section: dict[str, list[StudentQuestionView]] = {}
    for v in views:
        by_section.setdefault(v.section_id, []).append(v)
    titles = {s.id: s for s in bp.sections}
    ordered_ids = [s.id for s in bp.sections if s.id in by_section] + [k for k in by_section if k not in titles]
    for sid in ordered_ids:
        qs = by_section[sid]
        meta = titles.get(sid)
        heading = meta.title if meta else f"Section {sid}"
        total = sum(q.marks for q in qs)
        lines = [f"*{len(qs)} question{'s' if len(qs) != 1 else ''}, {total:g} marks.*"]
        if meta and meta.instructions:
            lines.append(f"*{meta.instructions}*")
        for q in qs:
            block = f"**Q{q.number}.** {q.text}  \n*[{_fmt_marks(q.marks)}]*"
            if q.type == "mcq":
                block += "\n\n" + "\n".join(f"- ({chr(65 + i)}) {c}" for i, c in enumerate(q.choices))
            elif q.type == "true_false":
                block += "\n\n- True\n- False"
            elif q.type in ("short_answer", "numerical"):
                block += "\n\n" + "\\_" * 60
            lines.append(block)
        sections.append(DocumentSection(heading=heading, markdown="\n\n".join(lines), level=2))
    return sections


def answer_key_sections(questions: list[Question], bp: Blueprint) -> list[DocumentSection]:
    ordered = sorted(questions, key=lambda q: (q.section_order, q.order_index))
    titles = {s.id: s.title for s in bp.sections}
    out: list[DocumentSection] = []
    current = None
    lines: list[str] = []
    for n, q in enumerate(ordered, start=1):
        if q.section_id != current:
            if lines:
                out.append(DocumentSection(heading=titles.get(current, current), markdown="\n\n".join(lines),
                                           level=2))
            current, lines = q.section_id, []
        a = q.answer or {}
        if q.type == "mcq" and "choice_label" in a:
            ans = f"({a['choice_label']}) {a.get('text', '')}"
        elif q.type == "numerical":
            ans = a.get("text") or f"{a.get('value')} {a.get('units') or ''}"
            if a.get("tolerance") is not None:
                ans += f" (tolerance ±{a['tolerance']:g})"
        else:
            ans = a.get("text", "")
        block = [f"**Q{n}.** {q.text}  \n*[{_fmt_marks(q.marks)} · {q.difficulty} · "
                 f"{TYPE_LABELS.get(q.type, q.type)}]*", f"**Answer:** {ans}"]
        if q.type == "coding" and a.get("reference_solution"):
            block.append(f"```{a.get('language') or 'python'}\n{a['reference_solution']}\n```")
        if q.explanation:
            block.append(f"**Explanation:** {q.explanation}")
        if q.rubric:
            rows = "\n".join(f"| {r['criterion'].replace('|', '/')} | {r['marks']:g} |" for r in q.rubric)
            block.append(f"**Marking scheme**\n\n| Criterion | Marks |\n|---|---|\n{rows}")
        flags = [f for f in (q.flags or []) if f.get("severity") in ("error", "warning")]
        if flags:
            block.append("> Review note: " + "; ".join(f["message"] for f in flags[:4]))
        if q.source_refs:
            block.append("*Based on:* " + "; ".join(r.get("title", "") for r in q.source_refs[:3]))
        lines.append("\n\n".join(block))
    if lines:
        out.append(DocumentSection(heading=titles.get(current, current), markdown="\n\n".join(lines), level=2))
    return out


class ExportService:
    def __init__(self, c: "Container") -> None:
        self.c = c

    async def _notes(self, ws_id: str, notes_id: str | None, kind: str) -> NotesDocument:
        async with self.c.db.session() as s:
            if notes_id:
                doc = await s.get(NotesDocument, notes_id)
            else:
                doc = (await s.execute(select(NotesDocument).where(
                    NotesDocument.workspace_id == ws_id, NotesDocument.kind == kind, NotesDocument.is_current))
                       ).scalar_one_or_none()
        if doc is None or doc.workspace_id != ws_id:
            raise NotFoundError(f"No {kind} notes found; generate notes first")
        if doc.kind != kind:
            raise ValidationFailed(f"Selected notes are {doc.kind} notes, not {kind} notes")
        return doc

    async def _assessment(self, ws_id: str, assessment_id: str | None) -> tuple[Assessment, list[Question]]:
        if not assessment_id:
            raise ValidationFailed("assessment_id is required")
        async with self.c.db.session() as s:
            a = await s.get(Assessment, assessment_id)
            if a is None or a.workspace_id != ws_id:
                raise NotFoundError("Assessment not found")
            if a.status != "ready":
                raise ValidationFailed("Assessment is not ready")
            qs = (await s.execute(select(Question).where(Question.assessment_id == a.id))).scalars().all()
        return a, list(qs)

    def _meta(self, ws: Workspace, req: ExportRequest, title: str, **kw: Any) -> DocumentMeta:
        return DocumentMeta(title=title, subject=ws.topic, class_name=req.class_name or ws.academic_level,
                            teacher=req.teacher_name, institution=req.institution,
                            date=req.date or datetime.now(UTC).date().isoformat(), **kw)

    async def build_spec(self, ws_id: str, req: ExportRequest) -> tuple[DocumentSpec, dict[str, Any]]:
        async with self.c.db.session() as s:
            ws = await s.get(Workspace, ws_id)
        if ws is None:
            raise NotFoundError("Workspace not found")
        if req.kind in ("student_notes", "teacher_notes"):
            kind = req.kind.split("_")[0]
            doc = await self._notes(ws_id, req.notes_id, kind)
            sections = [DocumentSection(heading=sec["heading"], markdown=sec["markdown"], level=1,
                                        page_break_before=(sec["heading"] == "References"))
                        for sec in doc.sections]
            meta = self._meta(ws, req, doc.title,
                              subtitle="Teacher notes" if kind == "teacher" else "Student notes",
                              footer_note="Teacher copy" if kind == "teacher" else "")
            return DocumentSpec(meta=meta, sections=sections), {"notes_id": doc.id, "version": doc.version}

        if req.kind in ("question_paper", "answer_key"):
            a, qs = await self._assessment(ws_id, req.assessment_id)
            cfg = AssessmentConfig.model_validate(a.config)
            bp = Blueprint.model_validate(a.blueprint)
            dur = f"{cfg.duration_minutes} minutes" if cfg.duration_minutes else ""
            total = f"{sum(q.marks for q in qs):g}"
            if req.kind == "question_paper":
                instructions = ["Answer all questions.", "The marks for each question are shown in brackets."]
                if cfg.custom_instructions:
                    instructions.append(cfg.custom_instructions)
                meta = self._meta(ws, req, cfg.title, duration=dur, total_marks=total, instructions=instructions)
                meta.class_name = req.class_name or cfg.class_name or meta.class_name
                meta.institution = req.institution or cfg.institution
                meta.teacher = req.teacher_name or cfg.teacher_name
                sections = paper_sections(student_view(qs), bp)
                if cfg.include_answers_in_paper:  # only when the teacher explicitly asked for it
                    keys = answer_key_sections(qs, bp)
                    if keys:
                        sections.append(DocumentSection(heading="Answer key", markdown="", level=1,
                                                        page_break_before=True))
                        sections += keys
                return DocumentSpec(meta=meta, sections=sections), {"assessment_id": a.id,
                                                                    "answers_included": cfg.include_answers_in_paper}
            meta = self._meta(ws, req, f"{cfg.title}: Answer Key and Marking Scheme", duration=dur,
                              total_marks=total, footer_note="CONFIDENTIAL - teacher use only")
            meta.class_name = req.class_name or cfg.class_name or meta.class_name
            return DocumentSpec(meta=meta, sections=answer_key_sections(qs, bp)), {"assessment_id": a.id}

        if req.kind == "student_pack":
            doc = await self._notes(ws_id, req.notes_id, "student")
            sections = [DocumentSection(heading=sec["heading"], markdown=sec["markdown"], level=1)
                        for sec in doc.sections]
            refs: dict[str, Any] = {"notes_id": doc.id}
            if req.assessment_id:
                a, qs = await self._assessment(ws_id, req.assessment_id)
                bp = Blueprint.model_validate(a.blueprint)
                sections.append(DocumentSection(heading=f"Practice test: {a.title}", markdown="", level=1,
                                                page_break_before=True))
                sections += paper_sections(student_view(qs), bp)  # never answers in a student pack
                refs["assessment_id"] = a.id
            meta = self._meta(ws, req, f"{ws.topic}: Student Pack", subtitle="Notes and practice")
            return DocumentSpec(meta=meta, sections=sections), refs
        raise ValidationFailed(f"Unknown export kind {req.kind}")

    async def export(self, ws_id: str, req: ExportRequest) -> Artifact:
        spec, refs = await self.build_spec(ws_id, req)
        fingerprint = sha256_text(req.kind + json.dumps(spec.model_dump(), sort_keys=True, default=str))
        idem = f"{req.kind}:{fingerprint[:40]}"
        async with self.c.db.session() as s:
            existing = (await s.execute(select(Artifact).where(Artifact.workspace_id == ws_id,
                                                               Artifact.idempotency_key == idem))
                        ).scalar_one_or_none()
        if existing is not None and await self.c.storage.exists(existing.storage_path):
            return existing
        data = await asyncio.to_thread(self.c.pdf.render, spec)
        key = f"{ws_id}/artifacts/{req.kind}-{fingerprint[:16]}.pdf"
        await self.c.storage.put(key, data, "application/pdf")
        async with self.c.db.session() as s:
            if existing is not None:
                row = await s.get(Artifact, existing.id)
                row.storage_path, row.size_bytes, row.checksum = key, len(data), _sha256_bytes(data)
                return row
            art = Artifact(workspace_id=ws_id, kind=req.kind, title=spec.meta.title, storage_path=key,
                           content_type="application/pdf", size_bytes=len(data),
                           checksum=_sha256_bytes(data), idempotency_key=idem,
                           refs={**refs, "provider": self.c.pdf.name})
            s.add(art)
            await s.flush()
            return art


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
