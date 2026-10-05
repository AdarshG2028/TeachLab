"""Coverage tracking and gap detection.

Coverage stages are recorded independently with timestamps:
planned (in roadmap) / researched (stored sources discuss it) / explained (assistant explained it) /
discussed (teacher asked about it) / completed (teacher explicitly marked it). Completion is never inferred.
Self-reported understanding is stored separately and is not treated as mastery.

Gap detection reports factual counts and concrete gaps, not mastery percentages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ValidationFailed
from app.db.models import CoverageRecord, NotesDocument, ResearchSession

if TYPE_CHECKING:
    from app.container import Container

STAGES = ("planned", "researched", "explained", "discussed", "completed")


class CoverageService:
    def __init__(self, c: Container) -> None:
        self.c = c

    async def resolve_concepts(self, workspace_id: str, names: list[str]) -> dict[str, str]:
        """name -> concept key (existing key when the name, or a variant of it, is already known)."""
        if not names:
            return {}
        index = await self.c.roadmap.concept_index(workspace_id)
        return {n: self.c.roadmap.concept_key(workspace_id, n, index) for n in names if n and n.strip()}

    async def _upsert(self, workspace_id: str, key: str, name: str, values: dict[str, Any],
                      append_message: str | None = None) -> None:
        insert_values = {"workspace_id": workspace_id, "concept_key": key, "concept_name": name[:300], **values}
        update_values: dict[str, Any] = dict(values)
        if append_message:
            insert_values["evidence_message_ids"] = [append_message]
            update_values["evidence_message_ids"] = CoverageRecord.__table__.c.evidence_message_ids.op("||")(
                cast([append_message], JSONB))
        async with self.c.db.session() as s:
            stmt = pg_insert(CoverageRecord).values(**insert_values)
            await s.execute(stmt.on_conflict_do_update(index_elements=["workspace_id", "concept_key"],
                                                       set_=update_values))

    async def record_turn(self, workspace_id: str, *, discussed: list[str], explained: list[str],
                          completed: list[str], feedback: list[dict], message_id: str) -> None:
        now = datetime.now(UTC)
        keys = await self.resolve_concepts(workspace_id, [*discussed, *explained, *completed,
                                                          *[f["concept"] for f in feedback]])
        for n in discussed:
            if n in keys:
                await self._upsert(workspace_id, keys[n], n, {"discussed_at": now}, message_id)
        for n in explained:
            if n in keys:
                await self._upsert(workspace_id, keys[n], n, {"explained_at": now}, message_id)
        for n in completed:
            if n in keys:
                await self._upsert(workspace_id, keys[n], n, {"completed_at": now, "completed_origin": "explicit"},
                                   message_id)
        for f in feedback:
            n = f["concept"]
            if n in keys:
                await self._upsert(workspace_id, keys[n], n, {"self_reported_understanding": f["understanding"],
                                                              "teacher_feedback": f.get("note", "")[:1000]},
                                   message_id)

    async def mark(self, workspace_id: str, concept_key: str, concept_name: str, stage: str, value: bool) -> None:
        """Explicit teacher marking (e.g. 'mark as completed' button)."""
        if stage not in ("completed", "explained", "discussed"):
            raise ValidationFailed("Only completed/explained/discussed can be marked manually")
        vals: dict[str, Any] = {f"{stage}_at": datetime.now(UTC) if value else None}
        if stage == "completed":
            vals["completed_origin"] = "explicit" if value else None
        await self._upsert(workspace_id, concept_key, concept_name, vals)

    async def records(self, workspace_id: str) -> dict[str, CoverageRecord]:
        async with self.c.db.session() as s:
            rows = (await s.execute(select(CoverageRecord).where(CoverageRecord.workspace_id == workspace_id))
                    ).scalars().all()
        return {r.concept_key: r for r in rows}

    async def covered_concept_keys(self, workspace_id: str, min_stage: str = "researched") -> list[str]:
        """Concepts at or beyond a stage (any of researched/explained/discussed/completed counts as >= researched)."""
        recs = await self.records(workspace_id)
        allowed = {"researched": ("researched_at", "explained_at", "discussed_at", "completed_at"),
                   "explained": ("explained_at", "completed_at"),
                   "completed": ("completed_at",)}[min_stage]
        return [k for k, r in recs.items() if any(getattr(r, a) for a in allowed)]

    async def refresh_researched(self, workspace_id: str, concepts: list[tuple[str, str]]) -> dict[str, int]:
        """Mark concepts as researched when stored passages really discuss them.

        Returns concept key -> number of distinct sources with relevant passages (0 = not researched).
        """
        now = datetime.now(UTC)
        support: dict[str, int] = {}
        for key, name in concepts:
            if key in support:
                continue
            passages = await self.c.retrieval.relevant_for_concept(workspace_id, name)
            support[key] = len({p.source_id for p in passages})
            if passages:
                await self._upsert(workspace_id, key, name, {"researched_at": now})
        return support

    # ------------------------------------------------------------------ gap detection

    async def gaps(self, workspace_id: str) -> dict[str, Any]:
        rm = await self.c.roadmap.current(workspace_id)
        recs = await self.records(workspace_id)
        gaps: list[dict[str, Any]] = []
        items = [(ph, it) for ph in (rm.phases if rm else []) if ph.included for it in ph.items if it.included]
        planned_keys = {it.concept_key for _, it in items}

        async with self.c.db.session() as s:
            notes = (await s.execute(select(NotesDocument.concept_keys).where(
                NotesDocument.workspace_id == workspace_id, NotesDocument.is_current))).scalars().all()
            sessions = (await s.execute(select(ResearchSession.state).where(
                ResearchSession.workspace_id == workspace_id))).scalars().all()
        noted = {k for ks in notes for k in (ks or [])}
        support = await self.refresh_researched(workspace_id, [(it.concept_key, it.title) for _, it in items])

        # 1. prerequisites the plan itself declares but no phase covers
        for _, it in items:
            for pk in it.prerequisite_keys or []:
                if pk not in planned_keys:
                    pname = recs[pk].concept_name if pk in recs else None
                    if pname:
                        gaps.append({"type": "missing_prerequisite", "kind": "required", "severity": "high",
                                     "concept_key": pk, "concept_name": pname,
                                     "detail": f"'{pname}' is listed as a prerequisite of '{it.title}' but no phase "
                                               "covers it.",
                                     "suggested_queries": [f"{pname} introduction"]})

        # 2. research depth, examples, materials and coverage per planned concept
        for ph, it in items:
            rec = recs.get(it.concept_key)
            n_sources = support.get(it.concept_key, 0)
            if n_sources == 0:
                gaps.append({"type": "under_researched", "kind": "required", "severity": "high",
                             "concept_key": it.concept_key, "concept_name": it.title,
                             "detail": f"No researched source discusses '{it.title}' (phase '{ph.title}').",
                             "suggested_queries": [f"{it.title} explained"]})
            elif n_sources == 1:
                gaps.append({"type": "weak_source_coverage", "kind": "recommended", "severity": "medium",
                             "concept_key": it.concept_key, "concept_name": it.title,
                             "detail": f"Only one source discusses '{it.title}'; consider corroborating.",
                             "suggested_queries": [f"{it.title} overview"]})
            if not (it.examples or []):
                gaps.append({"type": "missing_examples", "kind": "recommended", "severity": "low",
                             "concept_key": it.concept_key, "concept_name": it.title,
                             "detail": f"No teaching examples recorded for '{it.title}'.",
                             "suggested_queries": [f"{it.title} real-world examples"]})
            if it.concept_key not in noted:
                gaps.append({"type": "missing_teaching_material", "kind": "recommended", "severity": "low",
                             "concept_key": it.concept_key, "concept_name": it.title,
                             "detail": f"No current notes cover '{it.title}'.", "suggested_queries": []})
            if rec is None or not (rec.explained_at or rec.discussed_at or rec.completed_at):
                gaps.append({"type": "planned_not_covered", "kind": "info", "severity": "low",
                             "concept_key": it.concept_key, "concept_name": it.title,
                             "detail": f"'{it.title}' is planned but has not been explained, discussed or "
                                       "marked complete.", "suggested_queries": []})

        # 3. unresolved questions from conversations
        for st in sessions:
            for q in (st or {}).get("unresolved_questions", [])[:10]:
                gaps.append({"type": "unresolved_question", "kind": "recommended", "severity": "medium",
                             "concept_name": q[:200], "detail": q, "suggested_queries": [q[:200]]})

        counts = {
            "planned_items": len(items),
            "items_with_sources": sum(1 for _, it in items if support.get(it.concept_key)),
            "items_with_2plus_sources": sum(1 for _, it in items if support.get(it.concept_key, 0) >= 2),
            "items_explained": sum(1 for _, it in items if recs.get(it.concept_key)
                                   and recs[it.concept_key].explained_at),
            "items_completed_explicitly": sum(1 for _, it in items if recs.get(it.concept_key)
                                              and recs[it.concept_key].completed_at),
        }
        return {"counts": counts, "gaps": gaps,
                "note": "Counts describe recorded research and coverage events; they are not a measure of mastery."}
