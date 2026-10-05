"""Roadmaps: versioned teaching plans planned from the research.

A roadmap is ordered phases of concept items. Every change creates a new version. Teacher edits are
flagged (``teacher_edited``) and carried over when the roadmap is regenerated after new research.
Phase identity across versions uses a stable ``key``; concept identity uses a key derived from the
normalized concept name (shared with coverage records and test topics).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.concepts import concept_key as base_concept_key
from app.core.concepts import concept_name_keys
from app.core.errors import NotFoundError, ValidationFailed
from app.core.ids import new_id, normalize_key, stable_id
from app.core.logging import get_logger
from app.core.tokens import fit_items
from app.db.locks import workspace_lock
from app.db.models import CoverageRecord, ResearchSession, RoadmapItem, RoadmapPhase, RoadmapVersion, Workspace
from app.llm.structured import UNTRUSTED_POLICY, generate_structured, output_budget, prompt_budget, untrusted
from app.services.conversation.memory import session_state

if TYPE_CHECKING:
    from app.container import Container

log = get_logger(__name__)


class DraftItem(BaseModel):
    concept: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1200)
    objectives: list[str] = Field(default_factory=list, max_length=6)
    examples: list[str] = Field(default_factory=list, max_length=5)
    prerequisites: list[str] = Field(default_factory=list, max_length=6)
    optional: bool = False
    estimated_minutes: int | None = Field(default=None, ge=1, le=600)


class DraftPhase(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1500)
    objectives: list[str] = Field(default_factory=list, max_length=8)
    estimated_minutes: int | None = Field(default=None, ge=1, le=3000)
    items: list[DraftItem] = Field(min_length=1, max_length=15)


class RoadmapDraft(BaseModel):
    organization: Literal["prerequisite", "chronological", "thematic", "causal", "mixed"] = "prerequisite"
    summary: str = Field(default="", max_length=2000)
    phases: list[DraftPhase] = Field(min_length=1, max_length=12)


ROADMAP_SYSTEM = """You design teaching roadmaps for a teacher. Choose the organization that fits the subject:
technical subjects usually follow prerequisites then applications; history often needs chronological, thematic or
causal organization; mix when appropriate. Divide the roadmap into coherent phases (3-8 typically), each with
learning objectives and an ordered list of concept items. Put prerequisites before the concepts that need them.
Base the concepts on the provided research passages and on concepts already covered; you may add essential
concepts that are missing, but keep names short and canonical. Respect teacher-locked phases/items exactly as given.
Adapt depth to the student audience and the available time. Do not invent sources.
""" + UNTRUSTED_POLICY


def phase_key(workspace_id: str, title: str) -> str:
    return stable_id(workspace_id, "phase", normalize_key(title))


class RoadmapService:
    def __init__(self, c: Container) -> None:
        self.c = c

    # ------------------------------------------------------------------ reads

    async def current(self, workspace_id: str, s: AsyncSession | None = None) -> RoadmapVersion | None:
        q = (select(RoadmapVersion).where(RoadmapVersion.workspace_id == workspace_id, RoadmapVersion.is_current)
             .options(selectinload(RoadmapVersion.phases).selectinload(RoadmapPhase.items)))
        if s is not None:
            return (await s.execute(q)).scalar_one_or_none()
        async with self.c.db.session() as s2:
            return (await s2.execute(q)).scalar_one_or_none()

    async def phase_context(self, workspace_id: str, state: dict[str, Any]) -> dict[str, Any]:
        key = state.get("current_phase_id")
        if not key:
            return {}
        rm = await self.current(workspace_id)
        if rm is None:
            return {}
        for ph in rm.phases:
            if ph.key == key:
                return {"current_phase_title": ph.title, "objectives": ph.objectives,
                        "concepts": [i.title for i in ph.items if i.included]}
        return {}

    # ------------------------------------------------------------------ concept keys

    async def concept_index(self, workspace_id: str) -> dict[str, str]:
        """Normalized name -> existing concept key (coverage records), so renamed/plural forms stay linked."""
        async with self.c.db.session() as s:
            rows = (await s.execute(select(CoverageRecord.concept_key, CoverageRecord.concept_name).where(
                CoverageRecord.workspace_id == workspace_id))).all()
        idx: dict[str, str] = {}
        for key, name in rows:
            for k in concept_name_keys(name):
                idx.setdefault(k, key)
        return idx

    @staticmethod
    def concept_key(workspace_id: str, name: str, index: dict[str, str]) -> str:
        for k in concept_name_keys(name):
            if k in index:
                return index[k]
        return base_concept_key(workspace_id, name)

    # ------------------------------------------------------------------ generation

    async def draft(self, workspace_id: str, *, instructions: str = "", passages: list | None = None,
                    only_phase_key: str | None = None, progress=None) -> tuple[RoadmapDraft, RoadmapVersion | None]:
        """Plan phases from research passages and concepts already covered (not saved)."""
        async with self.c.db.session() as s:
            ws = await s.get(Workspace, workspace_id)
            if ws is None:
                raise NotFoundError("Workspace not found")
            prev = await self.current(workspace_id, s)
            covered = (await s.execute(select(CoverageRecord.concept_name).where(
                CoverageRecord.workspace_id == workspace_id).order_by(CoverageRecord.updated_at.desc()).limit(80))
                       ).scalars().all()
        if progress:
            await progress("reading research", 15)
        if passages is None:
            passages = await self.c.retrieval.search(workspace_id, f"{ws.topic} overview key concepts", top_k=12)
        if not passages and not covered:
            raise ValidationFailed("No research yet: research the topic before planning a roadmap")
        locked = self._locked_summary(prev, only_phase_key)
        out_tokens = output_budget(self.c.llm, "smart", 4000)
        material = prompt_budget(self.c.llm, "smart", out_tokens, overhead=1500)
        covered_text = ", ".join(fit_items(list(covered), int(material * 0.15))) or "(none yet)"
        passage_text = "\n\n".join(fit_items(
            [untrusted("source", p.text[:1500], title=p.title[:100]) for p in passages], int(material * 0.8)))
        user = (
            f"Topic: {ws.topic}\nScope: {ws.description or '-'}\nAudience: {ws.audience or '-'} "
            f"({ws.academic_level or '-'})\nTeaching goals: {'; '.join(ws.teaching_goals or []) or '-'}\n"
            f"Teacher familiarity level: {ws.familiarity_level} (0=beginner,1=familiar,2=advanced)\n"
            f"Time available: {ws.time_available_minutes or 'unspecified'} minutes\n"
            f"Language: {ws.language}\n\n"
            f"Concepts already covered in this workspace: {covered_text}\n\n"
            f"Research passages:\n{passage_text or '(none)'}\n\n"
            f"Teacher-locked phases/items to keep unchanged:\n{locked or '(none)'}\n\n"
            f"Teacher instructions: {instructions or '(none)'}"
        )
        if only_phase_key and prev:
            ph = next((p for p in prev.phases if p.key == only_phase_key), None)
            if ph is None:
                raise NotFoundError("Phase not found")
            user += (f"\n\nRegenerate ONLY the phase '{ph.title}' (return a roadmap containing just that one "
                     f"phase with improved items).")
        if progress:
            await progress("planning roadmap", 40)
        draft = await generate_structured(self.c.llm, task="roadmap", system=ROADMAP_SYSTEM, user=user,
                                          schema=RoadmapDraft, tier="smart", max_tokens=out_tokens)
        return draft, prev

    async def generate(self, workspace_id: str, *, instructions: str = "", progress=None,
                       only_phase_key: str | None = None, passages: list | None = None) -> RoadmapVersion:
        draft, prev = await self.draft(workspace_id, instructions=instructions, passages=passages,
                                       only_phase_key=only_phase_key, progress=progress)
        if progress:
            await progress("saving roadmap version", 80)
        index = await self.concept_index(workspace_id)
        version = await self._save_from_draft(workspace_id, draft, prev, index, only_phase_key, instructions)
        await self.c.coverage.refresh_researched(
            workspace_id, [(it.concept_key, it.title) for ph in version.phases for it in ph.items if it.included])
        return version

    @staticmethod
    def _locked_summary(prev: RoadmapVersion | None, only_phase_key: str | None) -> str:
        if prev is None:
            return ""
        lines = []
        for ph in prev.phases:
            if only_phase_key and ph.key == only_phase_key:
                continue
            if ph.teacher_edited or not ph.included:
                lines.append(f"Phase '{ph.title}' ({'excluded' if not ph.included else 'teacher edited'})")
            for it in ph.items:
                if it.teacher_edited or not it.included:
                    lines.append(f"  item '{it.title}' in '{ph.title}' "
                                 f"({'excluded' if not it.included else 'teacher edited'})")
        return "\n".join(lines)

    async def _save_from_draft(self, workspace_id: str, draft: RoadmapDraft, prev: RoadmapVersion | None,
                               index: dict[str, str], only_phase_key: str | None,
                               instructions: str) -> RoadmapVersion:
        new_phases: list[dict[str, Any]] = []
        for ph in draft.phases:
            items = [{
                "concept_key": self.concept_key(workspace_id, it.concept, index), "title": it.concept[:300],
                "description": it.description, "objectives": it.objectives, "examples": it.examples,
                "prerequisite_keys": [self.concept_key(workspace_id, p, index) for p in it.prerequisites],
                "optional": it.optional, "estimated_minutes": it.estimated_minutes, "included": True,
                "teacher_edited": False, "source_ids": [],
            } for it in ph.items]
            new_phases.append({"key": phase_key(workspace_id, ph.title), "title": ph.title,
                               "description": ph.description, "objectives": ph.objectives,
                               "estimated_minutes": ph.estimated_minutes, "included": True,
                               "teacher_edited": False, "status": "not_started", "items": items})

        if prev is not None:
            new_phases = self._merge_with_previous(workspace_id, prev, new_phases, only_phase_key)
        origin = "regenerated" if prev else "generated"
        return await self._write_version(workspace_id, new_phases, organization=draft.organization,
                                         summary=draft.summary, origin=origin,
                                         meta={"instructions": instructions, "only_phase": only_phase_key})

    @staticmethod
    def _phase_from_row(ph: RoadmapPhase) -> dict[str, Any]:
        return {"key": ph.key, "title": ph.title, "description": ph.description, "objectives": ph.objectives,
                "estimated_minutes": ph.estimated_minutes, "included": ph.included,
                "teacher_edited": ph.teacher_edited, "status": ph.status,
                "items": [{"concept_key": i.concept_key, "title": i.title, "description": i.description,
                           "objectives": i.objectives, "examples": i.examples,
                           "prerequisite_keys": i.prerequisite_keys, "optional": i.optional,
                           "estimated_minutes": i.estimated_minutes, "included": i.included,
                           "teacher_edited": i.teacher_edited, "source_ids": i.source_ids} for i in ph.items]}

    def _merge_with_previous(self, workspace_id: str, prev: RoadmapVersion, new_phases: list[dict],
                             only_phase_key: str | None) -> list[dict]:
        old = [self._phase_from_row(p) for p in prev.phases]
        if only_phase_key:
            # replace just one phase, keep everything else as-is (order, edits, statuses)
            replacement = new_phases[0]
            out = []
            for p in old:
                if p["key"] == only_phase_key:
                    replacement = {**replacement, "key": p["key"], "status": p["status"],
                                   "included": p["included"]}
                    kept = [i for i in p["items"] if i["teacher_edited"]]
                    kept_keys = {i["concept_key"] for i in kept}
                    replacement["items"] = kept + [i for i in replacement["items"] if i["concept_key"] not in kept_keys]
                    out.append(replacement)
                else:
                    out.append(p)
            return out

        old_by_key = {p["key"]: p for p in old}
        new_by_key = {p["key"]: p for p in new_phases}
        result: list[dict] = []
        for p in new_phases:
            prior = old_by_key.get(p["key"])
            if prior is None:
                result.append(p)
                continue
            p["status"] = prior["status"]
            if prior["teacher_edited"] or not prior["included"]:
                merged = {**prior}
                result.append(merged)
                continue
            old_items = {i["concept_key"]: i for i in prior["items"]}
            items = []
            for it in p["items"]:
                pi = old_items.get(it["concept_key"])
                items.append(pi if pi and (pi["teacher_edited"] or not pi["included"]) else it)
            seen = {i["concept_key"] for i in items}
            items += [i for i in prior["items"] if i["teacher_edited"] and i["concept_key"] not in seen]
            p["items"] = items
            result.append(p)
        # teacher-edited/excluded phases that the new draft dropped are preserved at their old position
        for idx, p in enumerate(old):
            if p["key"] not in new_by_key and (p["teacher_edited"] or not p["included"]):
                result.insert(min(idx, len(result)), p)
        # teacher-edited items whose phase vanished
        placed = {i["concept_key"] for p in result for i in p["items"]}
        orphans = [i for p in old for i in p["items"] if i["teacher_edited"] and i["concept_key"] not in placed]
        if orphans:
            result.append({"key": phase_key(workspace_id, "Teacher additions"), "title": "Teacher additions",
                           "description": "Items you added or edited that no longer fit the regenerated phases.",
                           "objectives": [], "estimated_minutes": None, "included": True, "teacher_edited": True,
                           "status": "not_started", "items": orphans})
        return result

    async def _write_version(self, workspace_id: str, phases: list[dict], *, organization: str, summary: str,
                             origin: str, meta: dict) -> RoadmapVersion:
        async with self.c.db.session() as s:
            await workspace_lock(s, workspace_id, "roadmap")
            last = (await s.execute(select(RoadmapVersion.version).where(RoadmapVersion.workspace_id == workspace_id)
                                    .order_by(RoadmapVersion.version.desc()).limit(1))).scalar_one_or_none() or 0
            await s.execute(update(RoadmapVersion).where(RoadmapVersion.workspace_id == workspace_id)
                            .values(is_current=False))
            ver = RoadmapVersion(workspace_id=workspace_id, version=last + 1, is_current=True,
                                 organization=organization, summary=summary, origin=origin, meta=meta)
            s.add(ver)
            await s.flush()
            seen_keys: set[str] = set()
            for pi, p in enumerate(phases):
                key = p["key"]
                if key in seen_keys:
                    key = stable_id(key, pi)
                seen_keys.add(key)
                ph = RoadmapPhase(version_id=ver.id, workspace_id=workspace_id, key=key, title=p["title"][:300],
                                  description=p.get("description", ""), objectives=p.get("objectives", []),
                                  order_index=pi, included=p.get("included", True),
                                  teacher_edited=p.get("teacher_edited", False),
                                  estimated_minutes=p.get("estimated_minutes"),
                                  status=p.get("status", "not_started"))
                s.add(ph)
                await s.flush()
                seen_items: set[str] = set()
                for ii, it in enumerate(p["items"]):
                    if it["concept_key"] in seen_items:
                        continue
                    seen_items.add(it["concept_key"])
                    s.add(RoadmapItem(phase_id=ph.id, workspace_id=workspace_id, concept_key=it["concept_key"],
                                      title=it["title"][:300], description=it.get("description", ""),
                                      objectives=it.get("objectives", []), examples=it.get("examples", []),
                                      prerequisite_keys=it.get("prerequisite_keys", []),
                                      source_ids=it.get("source_ids", []), order_index=ii,
                                      included=it.get("included", True), optional=it.get("optional", False),
                                      teacher_edited=it.get("teacher_edited", False),
                                      estimated_minutes=it.get("estimated_minutes")))
            await s.flush()
            # coverage: included items are "planned"
            now = datetime.now(UTC)
            for p in phases:
                if not p.get("included", True):
                    continue
                for it in p["items"]:
                    if not it.get("included", True):
                        continue
                    stmt = pg_insert(CoverageRecord).values(workspace_id=workspace_id, concept_key=it["concept_key"],
                                                            concept_name=it["title"][:300], planned_at=now)
                    await s.execute(stmt.on_conflict_do_update(
                        index_elements=["workspace_id", "concept_key"],
                        set_={"planned_at": stmt.excluded.planned_at}))
            ver_id = ver.id
        async with self.c.db.session() as s:
            return (await s.execute(select(RoadmapVersion).where(RoadmapVersion.id == ver_id).options(
                selectinload(RoadmapVersion.phases).selectinload(RoadmapPhase.items)))).scalar_one()

    # ------------------------------------------------------------------ teacher edits

    async def save_teacher_edit(self, workspace_id: str, phases_in: list[dict], expected_version: int | None
                                ) -> RoadmapVersion:
        """Persist an edited roadmap (full structure) as a new version, flagging what the teacher changed."""
        prev = await self.current(workspace_id)
        if prev is None:
            raise NotFoundError("No roadmap to edit")
        if expected_version is not None and expected_version != prev.version:
            raise ValidationFailed("Roadmap changed since you loaded it; reload and re-apply your edits")
        old_phases = {p.id: self._phase_from_row(p) for p in prev.phases}
        index = await self.concept_index(workspace_id)
        out: list[dict] = []
        for p in phases_in:
            prior = old_phases.get(p.get("id") or "")
            title = (p.get("title") or "").strip()
            if not title:
                raise ValidationFailed("Phase title required")
            fields = {"title": title, "description": p.get("description", ""),
                      "objectives": [o for o in p.get("objectives", []) if o][:10],
                      "estimated_minutes": p.get("estimated_minutes"), "included": bool(p.get("included", True))}
            if prior:
                changed = any(prior[k] != fields[k] for k in fields)
                phase = {**prior, **fields, "teacher_edited": prior["teacher_edited"] or changed}
            else:
                phase = {**fields, "key": phase_key(workspace_id, title + new_id()), "teacher_edited": True,
                         "status": "not_started"}
            prior_items = {i["concept_key"]: i for i in (prior["items"] if prior else [])}
            items = []
            for it in p.get("items", []):
                ititle = (it.get("title") or "").strip()
                if not ititle:
                    raise ValidationFailed("Item title required")
                ck = it.get("concept_key") or self.concept_key(workspace_id, ititle, index)
                ifields = {"title": ititle, "description": it.get("description", ""),
                           "objectives": it.get("objectives", [])[:8], "examples": it.get("examples", [])[:8],
                           "included": bool(it.get("included", True)), "optional": bool(it.get("optional", False)),
                           "estimated_minutes": it.get("estimated_minutes")}
                pi = prior_items.get(ck)
                if pi:
                    changed = any(pi.get(k) != ifields[k] for k in ifields)
                    items.append({**pi, **ifields, "concept_key": ck,
                                  "teacher_edited": pi["teacher_edited"] or changed})
                else:
                    items.append({**ifields, "concept_key": ck, "prerequisite_keys": [], "source_ids": [],
                                  "teacher_edited": True})
            if prior and [i["concept_key"] for i in items] != [i["concept_key"] for i in prior["items"]]:
                phase["teacher_edited"] = True  # reordered / removed items
            phase["items"] = items
            out.append(phase)
        old_order = [p.key for p in prev.phases]
        new_order = [p["key"] for p in out]
        if old_order != new_order:
            for p in out:
                p["teacher_edited"] = True if p["key"] in old_order and old_order.index(p["key"]) != new_order.index(
                    p["key"]) else p["teacher_edited"]
        return await self._write_version(workspace_id, out, organization=prev.organization, summary=prev.summary,
                                         origin="teacher_edit", meta={"based_on": prev.version})

    # ------------------------------------------------------------------ navigation (Level 0 tutorial flow)

    @staticmethod
    def _find_phase(phases: list[RoadmapPhase], ref: str | None) -> RoadmapPhase | None:
        if not ref:
            return None
        m = re.search(r"\d+", ref)
        if m:
            n = int(m.group(0))
            if 1 <= n <= len(phases):
                return phases[n - 1]
        key = normalize_key(ref)
        for ph in phases:
            if key and (key in normalize_key(ph.title) or normalize_key(ph.title) in key):
                return ph
        return None

    async def navigate(self, workspace_id: str, session_id: str, action: str, phase_ref: str | None = None
                       ) -> dict[str, Any]:
        async with self.c.db.session() as s:
            rm = await self.current(workspace_id, s)
            sess = (await s.execute(select(ResearchSession).where(
                ResearchSession.id == session_id, ResearchSession.workspace_id == workspace_id).with_for_update())
                    ).scalar_one_or_none()
            if sess is None:
                raise NotFoundError("Session not found")
            if rm is None:
                return {"ok": False, "reason": "no roadmap yet"}
            phases = [p for p in rm.phases if p.included]
            if not phases:
                return {"ok": False, "reason": "roadmap has no included phases"}
            state = session_state(sess)
            cur_idx = next((i for i, p in enumerate(phases) if p.key == state.get("current_phase_id")), None)
            cur = phases[cur_idx] if cur_idx is not None else None
            target: RoadmapPhase | None = None
            finished = False
            if action in ("start_phase", "goto_phase", "revisit_phase"):
                target = self._find_phase(phases, phase_ref) or (cur or phases[0])
            elif action == "next_phase":
                if cur is not None and cur.status in ("in_progress", "not_started"):
                    cur.status = "completed"  # moving on finishes the phase (concept mastery is not inferred)
                    state["completed_phase_ids"] = list(dict.fromkeys(state["completed_phase_ids"] + [cur.key]))
                if cur_idx is not None and cur_idx == len(phases) - 1:
                    finished = True
                    target = cur
                else:
                    target = phases[(cur_idx if cur_idx is not None else -1) + 1]
            elif action == "previous_phase":
                target = phases[max((cur_idx or 0) - 1, 0)]
            elif action == "skip_phase":
                skip = self._find_phase(phases, phase_ref) or cur or phases[0]
                skip.status = "skipped"
                state["skipped_phase_ids"] = list(dict.fromkeys(state["skipped_phase_ids"] + [skip.key]))
                idx = phases.index(skip)
                target = phases[min(idx + 1, len(phases) - 1)]
            elif action == "complete_phase":
                done = self._find_phase(phases, phase_ref) or cur
                if done is None:
                    return {"ok": False, "reason": "no current phase"}
                done.status = "completed"
                state["completed_phase_ids"] = list(dict.fromkeys(state["completed_phase_ids"] + [done.key]))
                state["skipped_phase_ids"] = [k for k in state["skipped_phase_ids"] if k != done.key]
                now = datetime.now(UTC)
                for it in done.items:
                    if it.included:
                        stmt = pg_insert(CoverageRecord).values(
                            workspace_id=workspace_id, concept_key=it.concept_key, concept_name=it.title,
                            completed_at=now, completed_origin="explicit")
                        await s.execute(stmt.on_conflict_do_update(
                            index_elements=["workspace_id", "concept_key"],
                            set_={"completed_at": now, "completed_origin": "explicit"}))
                idx = phases.index(done)
                finished = idx + 1 >= len(phases)
                target = phases[idx + 1] if not finished else done
            else:
                return {"ok": False, "reason": f"unknown action {action}"}
            if target is not None:
                revisiting_skipped = target.status == "skipped" and action in ("start_phase", "goto_phase",
                                                                               "revisit_phase")
                if target.status == "not_started" or revisiting_skipped:
                    target.status = "in_progress"
                state["current_phase_id"] = target.key
            sess.state = state
            return {"ok": True, "action": action, "current_phase": target.title if target else None,
                    "current_phase_key": target.key if target else None,
                    "phase_index": phases.index(target) + 1 if target else None, "phase_count": len(phases),
                    "finished": finished, "changed": target is not cur}

    async def set_phase_status(self, workspace_id: str, phase_key_: str, status: str) -> None:
        if status not in ("not_started", "in_progress", "completed", "skipped"):
            raise ValidationFailed("Invalid status")
        async with self.c.db.session() as s:
            rm = await self.current(workspace_id, s)
            if rm is None:
                raise NotFoundError("No roadmap")
            ph = next((p for p in rm.phases if p.key == phase_key_), None)
            if ph is None:
                raise NotFoundError("Phase not found")
            ph.status = status
