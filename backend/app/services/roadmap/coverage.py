"""Coverage tracking and gap detection.

Coverage stages are recorded independently with timestamps:
planned (in roadmap) / researched (graph claims with evidence) / explained (assistant explained it) /
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
    def __init__(self, c: "Container") -> None:
        self.c = c

    async def resolve_concepts(self, workspace_id: str, names: list[str]) -> dict[str, str]:
        """name -> concept key (existing graph entity id when known)."""
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

    # ------------------------------------------------------------------ gap detection

    async def gaps(self, workspace_id: str) -> dict[str, Any]:
        rm = await self.c.roadmap.current(workspace_id)
        recs = await self.records(workspace_id)
        outline = await self.c.graph.concept_outline(workspace_id, limit=250)
        node_by_id = {n["id"]: n for n in outline["nodes"]}
        gaps: list[dict[str, Any]] = []

        items = []
        if rm is not None:
            for ph in rm.phases:
                if not ph.included:
                    continue
                for it in ph.items:
                    if it.included:
                        items.append((ph, it))
        planned_keys = {it.concept_key for _, it in items}
        name_by_key = {it.concept_key: it.title for _, it in items}

        async with self.c.db.session() as s:
            notes = (await s.execute(select(NotesDocument.concept_keys).where(
                NotesDocument.workspace_id == workspace_id, NotesDocument.is_current))).scalars().all()
            sessions = (await s.execute(select(ResearchSession.state).where(
                ResearchSession.workspace_id == workspace_id))).scalars().all()
        noted = {k for ks in notes for k in (ks or [])}

        # 1. missing prerequisites: graph PREREQUISITE_OF edges into planned concepts from unplanned ones (required)
        for e in outline["edges"]:
            if e["family"] == "prerequisite" and e["target"] in planned_keys and e["source"] not in planned_keys:
                src = node_by_id.get(e["source"], {})
                gaps.append({"type": "missing_prerequisite", "kind": "required", "severity": "high",
                             "concept_key": e["source"], "concept_name": src.get("name", "?"),
                             "detail": f"'{src.get('name', '?')}' is a prerequisite of "
                                       f"'{name_by_key.get(e['target'], node_by_id.get(e['target'], {}).get('name'))}'"
                                       " (from researched sources) but is not in the roadmap.",
                             "suggested_queries": [f"{src.get('name', '')} explained for beginners"]})
        # roadmap-declared prerequisites that are missing from the plan (planner suggestion, lower certainty)
        for _, it in items:
            for pk in it.prerequisite_keys or []:
                if pk not in planned_keys:
                    pname = node_by_id.get(pk, {}).get("name") or (recs[pk].concept_name if pk in recs else None)
                    if pname:
                        gaps.append({"type": "missing_prerequisite", "kind": "recommended", "severity": "medium",
                                     "concept_key": pk, "concept_name": pname,
                                     "detail": f"The plan lists '{pname}' as a prerequisite of '{it.title}' but it "
                                               "is not covered by any phase.",
                                     "suggested_queries": [f"{pname} introduction"]})

        # 2-3. research depth per planned concept
        for ph, it in items:
            node = node_by_id.get(it.concept_key)
            rec = recs.get(it.concept_key)
            claims = node["claims"] if node else 0
            support = node["source_support"] if node else 0
            if not node or claims == 0:
                gaps.append({"type": "under_researched", "kind": "required", "severity": "high",
                             "concept_key": it.concept_key, "concept_name": it.title,
                             "detail": f"No researched evidence yet for '{it.title}' (phase '{ph.title}').",
                             "suggested_queries": [f"{it.title} {ph.title}".strip()]})
            elif support <= 1:
                gaps.append({"type": "weak_source_coverage", "kind": "recommended", "severity": "medium",
                             "concept_key": it.concept_key, "concept_name": it.title,
                             "detail": f"'{it.title}' is supported by only one source; consider corroborating.",
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

        # 4. claims needing review
        unsupported = await self.c.graph.list_claims(workspace_id, status="unverified", limit=50)
        contested = await self.c.graph.list_claims(workspace_id, status="contested", limit=50)
        for c in contested:
            gaps.append({"type": "contested_claim", "kind": "required", "severity": "medium", "claim_id": c["id"],
                         "concept_name": c["text"][:120],
                         "detail": "Sources disagree on this claim; review the evidence and perspectives.",
                         "suggested_queries": [c["text"][:150]]})
        for c in unsupported[:20]:
            gaps.append({"type": "unsupported_claim", "kind": "recommended", "severity": "low", "claim_id": c["id"],
                         "concept_name": c["text"][:120],
                         "detail": "No verbatim supporting quote was verified for this claim.",
                         "suggested_queries": [c["text"][:150]]})

        # 5. unresolved questions from conversations
        for st in sessions:
            for q in (st or {}).get("unresolved_questions", [])[:10]:
                gaps.append({"type": "unresolved_question", "kind": "recommended", "severity": "medium",
                             "concept_name": q[:200], "detail": q, "suggested_queries": [q[:200]]})

        # 6. optional extensions: well-connected researched concepts that are not planned
        for n in outline["nodes"][:60]:
            if n["id"] not in planned_keys and n["degree"] >= 3 and rm is not None:
                gaps.append({"type": "optional_extension", "kind": "optional", "severity": "info",
                             "concept_key": n["id"], "concept_name": n["name"],
                             "detail": f"'{n['name']}' is well connected in your research ({n['degree']} links) but "
                                       "not in the roadmap. Optional extension, not a requirement.",
                             "suggested_queries": []})

        counts = {
            "planned_items": len(items),
            "items_with_evidence": sum(1 for _, it in items if node_by_id.get(it.concept_key, {}).get("claims")),
            "items_with_2plus_sources": sum(1 for _, it in items
                                            if (node_by_id.get(it.concept_key, {}).get("source_support") or 0) >= 2),
            "items_explained": sum(1 for _, it in items if recs.get(it.concept_key)
                                   and recs[it.concept_key].explained_at),
            "items_completed_explicitly": sum(1 for _, it in items if recs.get(it.concept_key)
                                              and recs[it.concept_key].completed_at),
            "contested_claims": len(contested), "unverified_claims": len(unsupported),
        }
        return {"counts": counts, "gaps": gaps,
                "note": "Counts describe recorded research and coverage events; they are not a measure of mastery."}
