"""Assessment generation: scope -> blueprint -> generate -> validate -> repair -> persist; plus teacher edits.

Coverage modes:
- strict: only concepts that were actually covered (coverage >= researched, or in the selected notes) and
  that have evidence passages; every question must cite the provided passages and pass a similarity check.
- expanded: covered concepts plus closely related *researched* graph neighbours; questions may apply them to
  new scenarios or connect them, but must not introduce unresearched subject matter (flagged if they do).
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError, ValidationFailed
from app.core.ids import normalize_key
from app.core.logging import get_logger
from app.db.models import Assessment, NotesDocument, Question, Workspace
from app.llm.structured import UNTRUSTED_POLICY, generate_structured, untrusted
from app.services.assessment.blueprint import build_blueprint
from app.services.assessment.code_runner import run_python_tests
from app.services.assessment.schemas import (
    DEFAULT_MARKS,
    AssessmentConfig,
    Blueprint,
    BlueprintSlot,
    GenQuestion,
    GenQuestionBatch,
    StudentQuestionView,
)
from app.services.assessment.validator import (
    Issue,
    canonical_answer,
    normalized_rubric,
    text_duplicates,
    validate_structure,
    vector_duplicates,
)

if TYPE_CHECKING:
    from app.container import Container

log = get_logger(__name__)
_MARKER_RE = re.compile(r"S\d{1,3}")

TECH_HINTS = ("program", "code", "coding", "software", "python", "java", "algorithm", "data structure",
              "computer", "computing", "cloud", "web", "database", "machine learning", "iot", "devops", "api")
QUANT_HINTS = ("math", "physics", "chemistry", "statistic", "econom", "engineering", "finance", "calculus",
               "algebra", "geometry", "probability", "quantum", "accounting", "electric", "computing", "circuit")


def suggested_question_types(topic: str, description: str = "") -> dict[str, Any]:
    """Subject-appropriate defaults; the teacher may override them."""
    t = f"{topic} {description}".lower()
    types = ["mcq", "true_false", "fill_blank", "short_answer", "long_answer", "application"]
    reasons = {}
    if any(h in t for h in QUANT_HINTS):
        types.insert(4, "numerical")
        reasons["numerical"] = "quantitative subject"
    if any(h in t for h in TECH_HINTS):
        types.append("coding")
        reasons["coding"] = "technical / computing subject"
    return {"types": types, "reasons": reasons}


GEN_SYSTEM = f"""You write assessment questions for a teacher. You receive a list of blueprint slots; write exactly
one question per slot, keeping the slot_id, type, difficulty and target concept. Requirements:
- mcq: 4 plausible distinct choices, correct_choice = 0-based index, answer = the correct choice text.
- true_false: answer "True" or "False".
- fill_blank: put "_____" in the text where the answer goes; answer = the missing word(s).
- numerical: numeric_answer (number), units, tolerance, and answer_expression = a plain arithmetic expression
  using only numbers, + - * / ** and math functions (sqrt, log, exp, sin, cos, pi...) that evaluates to the answer.
- coding: answer = model solution explanation; reference_solution = Python code; code_tests = Python assert
  statements exercising it.
- short_answer / long_answer / application: answer = model answer; rubric = criteria whose marks sum to the
  slot marks.
- explanation: why the answer is correct (for the answer key).
- cited_sources: markers (e.g. "S2") of the passages the question is based on.
- concepts: the concept names the question assesses.
Write for the stated student level; make difficulty match the slot (easy = recall/understanding,
medium = application, hard = analysis/multi-step). Avoid duplicate or near-duplicate questions.
{UNTRUSTED_POLICY}"""

STRICT_RULE = ("COVERAGE MODE STRICT: every question must be answerable using only the provided passages and "
               "facts. Cite the passages you used.")
EXPANDED_RULE = ("COVERAGE MODE EXPANDED: questions may apply the covered concepts to new examples or connect "
                 "related covered concepts, but must not require knowledge of topics absent from the provided "
                 "material. Cite passages that underpin each question.")


class AssessmentService:
    def __init__(self, c: "Container") -> None:
        self.c = c
        self.settings = c.settings

    # ------------------------------------------------------------------ scope

    async def compute_scope(self, ws_id: str, cfg: AssessmentConfig) -> tuple[list[dict], dict[str, Any]]:
        report: dict[str, Any] = {}
        recs = await self.c.coverage.records(ws_id)
        if cfg.notes_ids:
            async with self.c.db.session() as s:
                notes = (await s.execute(select(NotesDocument).where(
                    NotesDocument.workspace_id == ws_id, NotesDocument.id.in_(cfg.notes_ids)))).scalars().all()
            if len(notes) != len(set(cfg.notes_ids)):
                raise ValidationFailed("Selected notes not found in this workspace")
            base = list(dict.fromkeys(k for n in notes for k in (n.concept_keys or [])))
            report["scope_source"] = "selected_notes"
        else:
            base = await self.c.coverage.covered_concept_keys(ws_id, "researched")
            report["scope_source"] = "coverage_records"
        if cfg.topics:
            requested = list(dict.fromkeys(cfg.topics))
            excluded = [t for t in requested if t not in base]
            if cfg.coverage_mode == "strict" and excluded:
                report["excluded_topics_not_covered"] = excluded
            base = [t for t in requested if t in base] if cfg.coverage_mode == "strict" else requested
        names: dict[str, str] = {}
        ents = await self.c.graph.entities_by_ids(ws_id, base)
        names.update({e["id"]: e["name"] for e in ents})
        for k in base:
            if k not in names and k in recs:
                names[k] = recs[k].concept_name
        scope = [{"key": k, "name": names[k], "related": False} for k in base if k in names]
        if cfg.coverage_mode == "expanded" and scope:
            keys = {c["key"] for c in scope}
            for c in list(scope)[:20]:
                try:
                    nb = await self.c.graph.neighborhood(ws_id, c["key"], depth=1, limit=8)
                except NotFoundError:
                    continue
                for n in nb["nodes"]:
                    if n["id"] not in keys:
                        keys.add(n["id"])
                        scope.append({"key": n["id"], "name": n["name"], "related": True})
            report["related_concepts_added"] = sum(1 for c in scope if c["related"])
        report["scope_size"] = len(scope)
        return scope, report

    async def concept_material(self, ws_id: str, concept: dict) -> tuple[list[dict], list[Any]]:
        facts = await self.c.graph.facts_for_entities(ws_id, [concept["key"]], limit=8)
        chunk_ids = await self.c.graph.evidence_chunks_for_entities(ws_id, [concept["key"]], limit=4)
        passages = await self.c.retrieval.chunks_by_ids(ws_id, chunk_ids)
        if len(passages) < 2:
            extra = await self.c.retrieval.search(ws_id, concept["name"], top_k=3,
                                                  min_score=self.settings.retrieval_min_score)
            seen = {p.chunk_id for p in passages}
            passages += [p for p in extra if p.chunk_id not in seen]
        return facts, passages[:4]

    # ------------------------------------------------------------------ lifecycle

    async def create(self, ws_id: str, cfg: AssessmentConfig) -> Assessment:
        async with self.c.db.session() as s:
            a = Assessment(workspace_id=ws_id, title=cfg.title, config=cfg.model_dump(), status="generating")
            s.add(a)
            await s.flush()
            return a

    async def generate(self, ws_id: str, assessment_id: str, progress=None) -> dict[str, Any]:
        async with self.c.db.session() as s:
            a = await s.get(Assessment, assessment_id)
            ws = await s.get(Workspace, ws_id)
        if a is None or a.workspace_id != ws_id or ws is None:
            raise NotFoundError("Assessment not found")
        cfg = AssessmentConfig.model_validate(a.config)
        try:
            return await self._generate(ws, a, cfg, progress)
        except Exception:
            async with self.c.db.session() as s:
                row = await s.get(Assessment, assessment_id)
                row.status = "failed"
            raise

    async def _generate(self, ws: Workspace, a: Assessment, cfg: AssessmentConfig, progress) -> dict[str, Any]:
        if progress:
            await progress("computing scope", 5)
        scope, scope_report = await self.compute_scope(ws.id, cfg)
        material: dict[str, tuple[list[dict], list[Any]]] = {}
        for c in scope:
            material[c["key"]] = await self.concept_material(ws.id, c)
        if cfg.coverage_mode == "strict":
            ungrounded = [c["name"] for c in scope if not material[c["key"]][1]]
            scope = [c for c in scope if material[c["key"]][1]]
            if ungrounded:
                scope_report["dropped_without_evidence"] = ungrounded
        blueprint = build_blueprint(a.id, cfg, scope)
        if progress:
            await progress("blueprint ready", 15, f"{len(blueprint.slots)} questions, {blueprint.total_marks:g} marks")

        generated: dict[str, GenQuestion] = {}
        markers_by_slot: dict[str, dict[str, Any]] = {}
        slots = blueprint.slots
        pending = list(slots)
        issues: dict[str, list[Issue]] = {}
        for round_no in range(3):  # initial generation + up to 2 repair rounds
            if not pending:
                break
            batches = [pending[i:i + 8] for i in range(0, len(pending), 8)]
            for bi, batch in enumerate(batches):
                if progress:
                    await progress("generating questions" if round_no == 0 else f"repair pass {round_no}",
                                   20 + int(55 * (bi + 1) / len(batches)) if round_no == 0 else 80)
                out, markers = await self._generate_batch(ws, cfg, batch, material, issues if round_no else {},
                                                          generated)
                for q in out:
                    generated[q.slot_id] = q
                    markers_by_slot[q.slot_id] = markers
            issues = await self._validate_all(cfg, blueprint, generated, markers_by_slot)
            pending = [sl for sl in slots if sl.slot_id not in generated
                       or any(i.severity == "error" for i in issues.get(sl.slot_id, []))]
            log.info("assessment_validation_round", round=round_no, pending=len(pending))

        if progress:
            await progress("verifying answers", 88)
        verification = await self._verify_code(generated)
        rows = self._to_rows(ws.id, a.id, blueprint, generated, issues, verification, markers_by_slot)
        report = self.report(cfg, blueprint, rows, scope_report)
        async with self.c.db.session() as s:
            await s.execute(delete(Question).where(Question.assessment_id == a.id))
            for r in rows:
                s.add(Question(**r))
            row = await s.get(Assessment, a.id)
            row.blueprint = blueprint.model_dump()
            row.validation_report = report
            row.total_marks = blueprint.total_marks
            row.status = "ready"
        return {"assessment_id": a.id, "questions": len(rows), "total_marks": blueprint.total_marks,
                "flagged": report["flagged_questions"]}

    async def _generate_batch(self, ws: Workspace, cfg: AssessmentConfig, batch: list[BlueprintSlot],
                              material: dict, prior_issues: dict[str, list[Issue]],
                              existing: dict[str, GenQuestion]) -> tuple[list[GenQuestion], dict[str, Any]]:
        markers: dict[str, Any] = {}
        fact_lines: list[str] = []
        for key in dict.fromkeys(sl.concept_key for sl in batch):
            facts, passages = material.get(key, ([], []))
            fact_lines += [f"- {f['text']}" for f in facts[:6]]
            for p in passages:
                if all(m.chunk_id != p.chunk_id for m in markers.values()):
                    markers[f"S{len(markers) + 1}"] = p
        slot_lines = []
        for sl in batch:
            line = (f"- slot_id={sl.slot_id}; type={sl.type}; marks={sl.marks:g}; difficulty={sl.difficulty}; "
                    f"concept={sl.concept_name}")
            if sl.slot_id in prior_issues:
                problems = "; ".join(i.message for i in prior_issues[sl.slot_id])
                line += f"\n  PREVIOUS ATTEMPT WAS REJECTED: {problems}. Write a new, corrected question."
            slot_lines.append(line)
        others = [q.text[:160] for sid, q in existing.items() if sid not in {sl.slot_id for sl in batch}][:40]
        user = (
            f"Subject: {ws.topic}\nStudent level: {cfg.student_level or ws.academic_level or ws.audience or '-'}\n"
            f"{STRICT_RULE if cfg.coverage_mode == 'strict' else EXPANDED_RULE}\n"
            f"Teacher instructions: {cfg.custom_instructions or '-'}\n\nSlots:\n" + "\n".join(slot_lines) +
            "\n\nAlready written questions (do not duplicate):\n" + ("\n".join(f"- {o}" for o in others) or "-") +
            "\n\nFacts:\n" + ("\n".join(fact_lines) or "-") + "\n\nPassages:\n" +
            "\n\n".join(untrusted("source", p.text, id=m, title=p.title[:100]) for m, p in markers.items()))
        out = await generate_structured(self.c.llm, task="questions", system=GEN_SYSTEM, user=user,
                                        schema=GenQuestionBatch, tier="smart", max_tokens=6000)
        wanted = {sl.slot_id for sl in batch}
        return [q for q in out.questions if q.slot_id in wanted], markers

    async def _validate_all(self, cfg: AssessmentConfig, bp: Blueprint, generated: dict[str, GenQuestion],
                            markers_by_slot: dict[str, dict]) -> dict[str, list[Issue]]:
        by_slot = {sl.slot_id: sl for sl in bp.slots}
        issues: dict[str, list[Issue]] = defaultdict(list)
        for sid, q in generated.items():
            issues[sid].extend(validate_structure(q, by_slot[sid]))
        # duplicates (exact, then semantic) -> regenerate the later one
        order = [sl.slot_id for sl in bp.slots if sl.slot_id in generated]
        texts = [generated[s].text for s in order]
        for i, j in text_duplicates(texts):
            issues[order[j]].append(Issue(order[j], "duplicate", f"Duplicates question {order[i]}", "error"))
        vectors = await asyncio.to_thread(self.c.embeddings.embed_documents, texts) if texts else []
        for i, j, sim in vector_duplicates(vectors, self.settings.duplicate_question_threshold):
            if not any(x.code == "duplicate" for x in issues[order[j]]):
                issues[order[j]].append(Issue(order[j], "duplicate",
                                              f"Near-duplicate of question {order[i]} (similarity {sim:.2f})",
                                              "error"))
        # grounding / scope
        scope_names = {normalize_key(c["name"]) for c in bp.scope_concepts}
        for idx, sid in enumerate(order):
            q = generated[sid]
            markers = markers_by_slot.get(sid, {})
            cited = [m for m in q.cited_sources if m in markers]
            if cfg.coverage_mode == "strict":
                if not cited:
                    issues[sid].append(Issue(sid, "ungrounded", "Strict mode: question must cite provided passages",
                                             "error"))
                else:
                    pv = await asyncio.to_thread(self.c.embeddings.embed_documents,
                                                 [markers[m].text for m in cited])
                    qa = vectors[idx] if idx < len(vectors) else None
                    if qa is not None:
                        best = max(sum(a * b for a, b in zip(qa, v, strict=True)) for v in pv)
                        if best < self.settings.grounding_min_score:
                            issues[sid].append(Issue(sid, "weak_grounding",
                                                     f"Question is weakly related to its cited passages "
                                                     f"(similarity {best:.2f})", "error"))
            foreign = [c for c in q.concepts if normalize_key(c) not in scope_names
                       and not any(normalize_key(c) in n or n in normalize_key(c) for n in scope_names)]
            if foreign:
                issues[sid].append(Issue(sid, "outside_scope_concept",
                                         f"Mentions concepts outside the covered material: {', '.join(foreign[:4])}",
                                         "warning"))
        return dict(issues)

    async def _verify_code(self, generated: dict[str, GenQuestion]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for sid, q in generated.items():
            if q.type != "coding" or not (q.reference_solution and q.code_tests):
                continue
            if not self.settings.code_verification_enabled or (q.code_language or "python").lower() != "python":
                out[sid] = {"status": "not_run", "reason": "code verification disabled or non-Python"}
                continue
            res = await run_python_tests(q.reference_solution, q.code_tests,
                                         self.settings.code_verification_timeout_seconds)
            out[sid] = {"status": "passed" if res.passed else "failed", "output": res.output[-1500:]}
        return out

    def _to_rows(self, ws_id: str, aid: str, bp: Blueprint, generated: dict[str, GenQuestion],
                 issues: dict[str, list[Issue]], verification: dict[str, dict],
                 markers_by_slot: dict[str, dict]) -> list[dict[str, Any]]:
        rows = []
        for sl in bp.slots:
            q = generated.get(sl.slot_id)
            flags = [i.as_flag() for i in issues.get(sl.slot_id, [])]
            if q is None:
                rows.append({"assessment_id": aid, "workspace_id": ws_id, "slot_id": sl.slot_id,
                             "section_id": sl.section_id, "section_order": sl.section_order,
                             "order_index": sl.order_index, "type": sl.type,
                             "text": f"[Generation failed for this slot - please write or regenerate a "
                                     f"{sl.type} question on {sl.concept_name}]",
                             "difficulty": sl.difficulty, "marks": sl.marks, "concept_keys": [sl.concept_key],
                             "flags": [{"code": "generation_failed", "message": "No valid question generated",
                                        "severity": "error"}], "origin": "generated"})
                continue
            rubric, rescaled = normalized_rubric(q, sl.marks)
            if rescaled:
                flags.append({"code": "rubric_rescaled", "message": "Rubric marks were rescaled to the question "
                                                                    "marks", "severity": "warning"})
            ver = dict(verification.get(sl.slot_id, {}))
            if q.type == "numerical":
                ver["numeric"] = "verified" if q.answer_expression and not any(
                    f["code"].startswith("numeric") for f in flags) else "unverified"
            if q.type == "coding" and ver.get("status") != "passed":
                flags.append({"code": "needs_teacher_review", "message": "Coding answer not executed/verified "
                                                                        "automatically", "severity": "warning"})
            markers = markers_by_slot.get(sl.slot_id, {})
            refs = [{"marker": m, "source_id": markers[m].source_id, "chunk_id": markers[m].chunk_id,
                     "title": markers[m].title} for m in q.cited_sources if m in markers]
            rows.append({"assessment_id": aid, "workspace_id": ws_id, "slot_id": sl.slot_id,
                         "section_id": sl.section_id, "section_order": sl.section_order, "order_index": sl.order_index,
                         "type": sl.type, "text": q.text.strip(),
                         "choices": [c.strip() for c in q.choices] if q.type == "mcq" else [],
                         "answer": canonical_answer(q), "explanation": q.explanation, "rubric": rubric,
                         "difficulty": q.difficulty, "marks": sl.marks, "concept_keys": [sl.concept_key],
                         "source_refs": refs, "flags": flags, "verification": ver, "origin": "generated"})
        return rows

    @staticmethod
    def report(cfg: AssessmentConfig, bp: Blueprint, rows: list[dict], scope_report: dict | None = None
               ) -> dict[str, Any]:
        section_totals: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            section_totals[r["section_id"]] += r["marks"]
            counts[r["section_id"]] += 1
        expected = {s.id: s for s in bp.sections}
        problems = []
        for sid, sec in expected.items():
            if counts.get(sid, 0) != sec.count:
                problems.append(f"Section {sid}: {counts.get(sid, 0)} questions, expected {sec.count}")
            if abs(section_totals.get(sid, 0) - sec.total) > 1e-6:
                problems.append(f"Section {sid}: {section_totals.get(sid, 0):g} marks, expected {sec.total:g}")
        total = round(sum(r["marks"] for r in rows), 2)
        target = cfg.total_marks if cfg.total_marks is not None else bp.total_marks
        if abs(total - target) > 1e-6:
            problems.append(f"Total marks {total:g} differ from configured {target:g}")
        flagged = [r["slot_id"] for r in rows if r.get("flags")]
        return {"question_count": len(rows), "expected_count": len(bp.slots), "total_marks": total,
                "expected_total_marks": target, "section_totals": dict(section_totals),
                "consistent": not problems, "problems": problems, "flagged_questions": len(flagged),
                "flagged_slots": flagged, "coverage_mode": cfg.coverage_mode, "scope": scope_report or {},
                "difficulty_counts": {d: sum(1 for r in rows if r["difficulty"] == d) for d in ("easy", "medium",
                                                                                               "hard")}}

    # ------------------------------------------------------------------ teacher editing

    async def get(self, ws_id: str, assessment_id: str) -> Assessment:
        async with self.c.db.session() as s:
            a = (await s.execute(select(Assessment).where(Assessment.id == assessment_id,
                                                          Assessment.workspace_id == ws_id)
                                 .options(selectinload(Assessment.questions)))).scalar_one_or_none()
        if a is None:
            raise NotFoundError("Assessment not found")
        return a

    async def refresh_report(self, ws_id: str, assessment_id: str) -> dict[str, Any]:
        a = await self.get(ws_id, assessment_id)
        cfg = AssessmentConfig.model_validate(a.config)
        bp = Blueprint.model_validate(a.blueprint) if a.blueprint else None
        if bp is None:
            return {}
        rows = [{"section_id": q.section_id, "marks": q.marks, "slot_id": q.slot_id, "flags": q.flags,
                 "difficulty": q.difficulty} for q in a.questions]
        report = self.report(cfg, bp, rows, (a.validation_report or {}).get("scope"))
        async with self.c.db.session() as s:
            row = await s.get(Assessment, assessment_id)
            row.validation_report = report
            row.total_marks = report["total_marks"]
        return report

    async def update_question(self, ws_id: str, question_id: str, patch: dict[str, Any]) -> Question:
        async with self.c.db.session() as s:
            q = await s.get(Question, question_id)
            if q is None or q.workspace_id != ws_id:
                raise NotFoundError("Question not found")
            merged = {
                "slot_id": q.slot_id, "type": patch.get("type", q.type), "text": patch.get("text", q.text),
                "choices": patch.get("choices", q.choices), "difficulty": patch.get("difficulty", q.difficulty),
                "explanation": patch.get("explanation", q.explanation),
                "rubric": patch.get("rubric", q.rubric),
            }
            answer = patch.get("answer", q.answer) or {}
            gq = GenQuestion(
                slot_id=q.slot_id, type=merged["type"], text=merged["text"], choices=merged["choices"],
                correct_choice=answer.get("choice_index"), answer=str(answer.get("text", "")),
                numeric_answer=answer.get("value"), tolerance=answer.get("tolerance"), units=answer.get("units"),
                answer_expression=answer.get("expression"), reference_solution=answer.get("reference_solution"),
                code_tests=answer.get("tests"), explanation=merged["explanation"], rubric=merged["rubric"],
                difficulty=merged["difficulty"])
            marks = float(patch.get("marks", q.marks))
            if marks <= 0:
                raise ValidationFailed("Marks must be positive")
            slot = BlueprintSlot(slot_id=q.slot_id, section_id=q.section_id, section_order=q.section_order,
                                 order_index=q.order_index, type=gq.type, marks=marks, difficulty=gq.difficulty,
                                 concept_key=(q.concept_keys or [""])[0], concept_name="")
            errors = [i for i in validate_structure(gq, slot) if i.severity == "error"]
            if errors:
                raise ValidationFailed("; ".join(e.message for e in errors),
                                       details={"issues": [e.as_flag() for e in errors]})
            rubric, _ = normalized_rubric(gq, marks)
            q.type, q.text, q.choices = gq.type, gq.text, gq.choices if gq.type == "mcq" else []
            q.answer, q.explanation, q.rubric = canonical_answer(gq), gq.explanation, rubric
            q.difficulty, q.marks, q.origin = gq.difficulty, marks, "edited"
            q.flags = [f for f in validate_structure(gq, slot) if f.severity == "warning"
                       and f.code != "difficulty_mismatch"]
            q.flags = [f.as_flag() for f in q.flags]
            await s.flush()
            aid = q.assessment_id
        await self.refresh_report(ws_id, aid)
        return q

    async def delete_question(self, ws_id: str, question_id: str) -> None:
        async with self.c.db.session() as s:
            q = await s.get(Question, question_id)
            if q is None or q.workspace_id != ws_id:
                raise NotFoundError("Question not found")
            aid = q.assessment_id
            await s.delete(q)
        await self.refresh_report(ws_id, aid)

    async def regenerate_question(self, ws_id: str, question_id: str, instructions: str = "") -> Question:
        async with self.c.db.session() as s:
            q = await s.get(Question, question_id)
            if q is None or q.workspace_id != ws_id:
                raise NotFoundError("Question not found")
            ws = await s.get(Workspace, ws_id)
        a = await self.get(ws_id, q.assessment_id)
        cfg = AssessmentConfig.model_validate(a.config)
        if instructions:
            cfg.custom_instructions = (cfg.custom_instructions + "\n" + instructions).strip()[:2000]
        bp = Blueprint.model_validate(a.blueprint)
        slot = next((sl for sl in bp.slots if sl.slot_id == q.slot_id), None)
        if slot is None:
            raise ValidationFailed("Question has no blueprint slot")
        slot = slot.model_copy(update={"marks": q.marks})
        concept = next((c for c in bp.scope_concepts if c["key"] == slot.concept_key),
                       {"key": slot.concept_key, "name": slot.concept_name})
        material = {slot.concept_key: await self.concept_material(ws_id, concept)}
        existing = {x.slot_id: GenQuestion(slot_id=x.slot_id, type=x.type, text=x.text) for x in a.questions
                    if x.id != q.id}
        prior = {slot.slot_id: [Issue(slot.slot_id, "replace", f"Teacher asked for a different question than: "
                                                              f"'{q.text[:200]}'", "error")]}
        last_issues: list[Issue] = []
        for _ in range(3):
            out, markers = await self._generate_batch(ws, cfg, [slot], material, prior, existing)
            if not out:
                continue
            new = out[0]
            last_issues = validate_structure(new, slot)
            if any(normalize_key(new.text) == normalize_key(x.text) for x in existing.values()):
                last_issues.append(Issue(slot.slot_id, "duplicate", "Duplicates another question", "error"))
            if not any(i.severity == "error" for i in last_issues):
                break
            prior = {slot.slot_id: last_issues}
        else:
            raise ValidationFailed("Could not generate a valid replacement question; try different instructions")
        rows = self._to_rows(ws_id, a.id, Blueprint(sections=bp.sections, slots=[slot], total_marks=slot.marks,
                                                    scope_concepts=bp.scope_concepts, coverage_mode=bp.coverage_mode),
                             {slot.slot_id: new}, {slot.slot_id: last_issues},
                             await self._verify_code({slot.slot_id: new}), {slot.slot_id: markers})
        row = rows[0]
        async with self.c.db.session() as s:
            target = await s.get(Question, question_id)
            for k in ("text", "choices", "answer", "explanation", "rubric", "difficulty", "source_refs", "flags",
                      "verification"):
                setattr(target, k, row[k])
            target.origin = "regenerated"
        await self.refresh_report(ws_id, a.id)
        async with self.c.db.session() as s:
            return await s.get(Question, question_id)

    async def add_manual_question(self, ws_id: str, assessment_id: str, data: dict[str, Any]) -> Question:
        a = await self.get(ws_id, assessment_id)
        section_id = data.get("section_id") or (a.questions[-1].section_id if a.questions else "S1")
        same = [q for q in a.questions if q.section_id == section_id]
        qtype = data.get("type", "short_answer")
        async with self.c.db.session() as s:
            q = Question(assessment_id=a.id, workspace_id=ws_id, slot_id=f"{section_id}.m{len(same) + 1}",
                         section_id=section_id, section_order=same[0].section_order if same else len(a.questions),
                         order_index=(max((x.order_index for x in same), default=-1) + 1), type=qtype,
                         text="(draft)", difficulty=data.get("difficulty", "medium"),
                         marks=float(data.get("marks") or DEFAULT_MARKS.get(qtype, 1)), origin="manual")
            s.add(q)
            await s.flush()
            qid = q.id
        try:
            return await self.update_question(ws_id, qid, data)
        except Exception:
            async with self.c.db.session() as s:
                await s.execute(delete(Question).where(Question.id == qid))
            raise


def student_view(questions: list[Question]) -> list[StudentQuestionView]:
    """Project questions to the answer-free student representation, numbered in paper order."""
    ordered = sorted(questions, key=lambda q: (q.section_order, q.order_index))
    return [StudentQuestionView(number=str(i), section_id=q.section_id, type=q.type, text=q.text,
                                choices=list(q.choices or []), marks=q.marks)
            for i, q in enumerate(ordered, start=1)]
