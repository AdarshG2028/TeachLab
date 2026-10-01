"""Knowledge-graph exploration, provenance, claim review and ontology proposal review."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import ContainerDep, WorkspaceDep
from app.api.schemas import ClaimReview, JobOut, ProposalOut, ProposalResolve
from app.core.errors import NotFoundError, ValidationFailed
from app.db.models import CoverageRecord, OntologyProposal, RelationType, RoadmapItem
from app.jobs.queue import enqueue
from app.services.knowledge.relations import REL_TYPE_RE, RelationRegistry
from app.services.knowledge.service import workspace_lock

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["graph"])


@router.get("/graph/overview")
async def graph_overview(w: WorkspaceDep, c: ContainerDep, limit: int = Query(default=150, ge=10, le=500)):
    return await c.graph.overview(w.id, limit)


@router.get("/graph/stats")
async def graph_stats(w: WorkspaceDep, c: ContainerDep):
    return await c.graph.stats(w.id)


@router.get("/graph/search")
async def graph_search(w: WorkspaceDep, c: ContainerDep, q: str = Query(min_length=1, max_length=200)):
    return await c.graph.search_entities(w.id, q)


@router.get("/graph/entities/{entity_id}")
async def entity_detail(entity_id: str, w: WorkspaceDep, c: ContainerDep):
    """Entity plus every claim about it, with evidence passages and sources (why it was added)."""
    return await c.graph.entity_detail(w.id, entity_id)


@router.get("/graph/entities/{entity_id}/neighborhood")
async def neighborhood(entity_id: str, w: WorkspaceDep, c: ContainerDep, depth: int = Query(default=1, ge=1, le=2)):
    return await c.graph.neighborhood(w.id, entity_id, depth)


@router.get("/graph/claims")
async def claims(w: WorkspaceDep, c: ContainerDep, status: str | None = None,
                 limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    return await c.graph.list_claims(w.id, status, limit, offset)


@router.get("/graph/claims/{claim_id}")
async def claim_detail(claim_id: str, w: WorkspaceDep, c: ContainerDep):
    return await c.graph.claim_detail(w.id, claim_id)


@router.post("/graph/claims/{claim_id}/review")
async def review_claim(claim_id: str, body: ClaimReview, w: WorkspaceDep, c: ContainerDep):
    await c.graph.set_claim_status(w.id, claim_id, body.status, body.note)
    return await c.graph.claim_detail(w.id, claim_id)


@router.post("/graph/rebuild", response_model=JobOut, status_code=202)
async def rebuild(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return await enqueue(s, kind="rebuild_graph", user_id=w.user_id, workspace_id=w.id, payload={})


@router.post("/vectors/reindex", response_model=JobOut, status_code=202)
async def reindex(w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        return await enqueue(s, kind="reindex_vectors", user_id=w.user_id, workspace_id=w.id, payload={})


@router.get("/ontology/proposals", response_model=list[ProposalOut])
async def proposals(w: WorkspaceDep, c: ContainerDep, status: str | None = "pending"):
    async with c.db.session() as s:
        q = select(OntologyProposal).where(OntologyProposal.workspace_id == w.id)
        if status:
            q = q.where(OntologyProposal.status == status)
        return (await s.execute(q.order_by(OntologyProposal.created_at.desc()).limit(300))).scalars().all()


@router.get("/ontology/relation-types")
async def relation_types(w: WorkspaceDep, c: ContainerDep) -> dict[str, Any]:
    reg = RelationRegistry.default()
    async with c.db.session() as s:
        ws_types = (await s.execute(select(RelationType).where(RelationType.workspace_id == w.id))).scalars().all()
    return {"canonical": [{"name": n, "family": sp.family, "symmetric": sp.symmetric,
                           "synonyms": list(sp.synonyms)[:8]} for n, sp in reg.specs.items()],
            "workspace": [{"name": t.name, "family": t.family, "status": t.status, "synonyms": t.synonyms}
                          for t in ws_types]}


@router.post("/ontology/proposals/{proposal_id}/resolve", response_model=ProposalOut)
async def resolve_proposal(proposal_id: str, body: ProposalResolve, w: WorkspaceDep, c: ContainerDep):
    async with c.db.session() as s:
        p = await s.get(OntologyProposal, proposal_id)
    if p is None or p.workspace_id != w.id:
        raise NotFoundError("Proposal not found")
    if p.status != "pending":
        raise ValidationFailed("Proposal already resolved")
    resolution: dict[str, Any] = {"decision": body.decision, "note": body.note}

    if p.kind == "new_relation_type":
        name = p.payload["name"]
        if body.decision == "accept":
            async with c.db.session() as s:
                await s.execute(update(RelationType).where(RelationType.workspace_id == w.id,
                                                           RelationType.name == name).values(status="accepted"))
            resolution["changed_edges"] = await c.graph.remap_relation_type(w.id, name, name, p.payload["family"],
                                                                             "accepted")
        else:
            target = (body.map_to or "RELATED_TO").upper()
            reg = RelationRegistry.default()
            if not REL_TYPE_RE.match(target) or target not in reg.specs:
                raise ValidationFailed("map_to must be a canonical relation type")
            async with c.db.session() as s:
                await s.execute(update(RelationType).where(RelationType.workspace_id == w.id,
                                                           RelationType.name == name).values(status="rejected"))
                # future extractions of the same phrase map to the chosen canonical type
                await s.execute(pg_insert(RelationType).values(
                    workspace_id=w.id, name=target, family=reg.specs[target].family, status="accepted",
                    synonyms=p.payload.get("examples", [])).on_conflict_do_update(
                    index_elements=["workspace_id", "name"],
                    set_={"synonyms": p.payload.get("examples", [])}))
            resolution["changed_edges"] = await c.graph.remap_relation_type(
                w.id, name, target, reg.specs[target].family, "canonical")
            resolution["mapped_to"] = target
    elif p.kind == "entity_merge" and body.decision == "accept":
        ids = p.payload["entity_ids"]
        keep = body.keep_entity_id or ids[0]
        if keep not in ids:
            raise ValidationFailed("keep_entity_id must be one of the proposal's entities")
        dup = next(i for i in ids if i != keep)
        async with c.db.session() as s:
            await workspace_lock(s, w.id)
            resolution.update(await c.graph.merge_entities(w.id, keep, dup))
            # keep relational references consistent
            await s.execute(update(RoadmapItem).where(RoadmapItem.workspace_id == w.id,
                                                      RoadmapItem.concept_key == dup).values(concept_key=keep))
            has_keep = (await s.execute(select(CoverageRecord.id).where(
                CoverageRecord.workspace_id == w.id, CoverageRecord.concept_key == keep))).scalar_one_or_none()
            if has_keep:
                await s.execute(delete(CoverageRecord).where(CoverageRecord.workspace_id == w.id,
                                                             CoverageRecord.concept_key == dup))
            else:
                await s.execute(update(CoverageRecord).where(CoverageRecord.workspace_id == w.id,
                                                             CoverageRecord.concept_key == dup)
                                .values(concept_key=keep))
        resolution["kept"], resolution["merged"] = keep, dup
    elif p.kind == "contradiction" and body.decision == "accept" and body.reject_claim_id:
        if body.reject_claim_id not in p.payload.get("claim_ids", []):
            raise ValidationFailed("reject_claim_id must be one of the contradicting claims")
        await c.graph.set_claim_status(w.id, body.reject_claim_id, "rejected", body.note)
        resolution["rejected_claim"] = body.reject_claim_id
    elif p.kind == "type_mismatch" and body.decision == "reject":
        resolution["note"] = body.note or "Relation kept; teacher may reject the underlying claim individually."

    async with c.db.session() as s:
        row = await s.get(OntologyProposal, proposal_id)
        row.status = "accepted" if body.decision == "accept" else "rejected"
        row.resolution = resolution
        row.resolved_at = datetime.now(UTC)
        await s.flush()
        return row
