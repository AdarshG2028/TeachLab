"""Knowledge pipeline: extract (LLM) -> persist raw extraction (PG) -> resolve (ontology) -> write (Neo4j).

Extraction rows are keyed by (workspace, batch_key) so retries never re-call the
LLM for a batch that already succeeded, and the graph can be rebuilt from
PostgreSQL without any LLM calls (``rebuild_graph``). Graph writes for one
workspace are serialized with a PostgreSQL advisory lock so concurrent jobs
cannot create duplicate entities.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import sha256_text
from app.core.logging import get_logger
from app.db.models import Chunk, CoverageRecord, Extraction, OntologyProposal, RelationType, Source
from app.db.session import Database
from app.providers.graph.neo4j_store import Neo4jGraphStore
from app.providers.llm.base import LLMProvider
from app.services.knowledge.extractor import PROMPT_VERSION, Passage, batch_passages, extract_candidates
from app.services.knowledge.ontology import OntologyResolver, PassageRef
from app.services.knowledge.relations import RelationRegistry
from app.services.knowledge.schemas import ExtractionResult, GraphWriteBatch

log = get_logger(__name__)


async def workspace_lock(session: AsyncSession, workspace_id: str, purpose: str = "graph") -> None:
    """Transaction-scoped advisory lock serializing graph writes per workspace."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{purpose}:{workspace_id}"})


async def load_registry(session: AsyncSession, workspace_id: str) -> RelationRegistry:
    reg = RelationRegistry.default()
    rows = (await session.execute(
        select(RelationType).where(RelationType.workspace_id == workspace_id,
                                   RelationType.status.in_(["accepted", "proposed"])))).scalars().all()
    for r in rows:
        reg.add_workspace_type(r.name, r.family, list(r.synonyms or []), r.status)
    return reg


class KnowledgeService:
    def __init__(self, db: Database, graph: Neo4jGraphStore, llm: LLMProvider, *, batch_tokens: int) -> None:
        self.db = db
        self.graph = graph
        self.llm = llm
        self.batch_tokens = batch_tokens

    async def extract_source(self, workspace_id: str, source_id: str, *, topic: str,
                             session_id: str | None) -> list[str]:
        """Run LLM extraction over a source's chunks; returns extraction ids (existing ones are reused)."""
        async with self.db.session() as s:
            src = await s.get(Source, source_id)
            if src is None or src.workspace_id != workspace_id:
                raise ValueError("source not in workspace")
            chunks = (await s.execute(select(Chunk).where(Chunk.source_id == source_id).order_by(Chunk.idx))
                      ).scalars().all()
            title = src.title or src.url or "source"
        passages = [Passage(pid="", chunk_id=c.id, source_id=source_id, source_title=title, text=c.text)
                    for c in chunks]
        ids: list[str] = []
        model = self.llm.model_for("fast")
        for batch in batch_passages(passages, self.batch_tokens):
            for i, p in enumerate(batch, start=1):
                p.pid = f"P{i}"
            chunk_ids = [p.chunk_id for p in batch]
            batch_key = sha256_text(f"{PROMPT_VERSION}|{model}|{','.join(chunk_ids)}")
            async with self.db.session() as s:
                existing = (await s.execute(select(Extraction.id).where(
                    Extraction.workspace_id == workspace_id, Extraction.batch_key == batch_key))).scalar_one_or_none()
            if existing:
                ids.append(existing)
                continue
            result = await extract_candidates(self.llm, topic=topic, passages=batch)
            async with self.db.session() as s:
                stmt = pg_insert(Extraction).values(
                    workspace_id=workspace_id, source_id=source_id, session_id=session_id, batch_key=batch_key,
                    chunk_ids=chunk_ids, model=model, prompt_version=PROMPT_VERSION,
                    payload={"result": result.model_dump(), "passages": {p.pid: p.chunk_id for p in batch}},
                ).on_conflict_do_nothing(index_elements=["workspace_id", "batch_key"]).returning(Extraction.id)
                new_id = (await s.execute(stmt)).scalar_one_or_none()
                if new_id is None:
                    new_id = (await s.execute(select(Extraction.id).where(
                        Extraction.workspace_id == workspace_id, Extraction.batch_key == batch_key))).scalar_one()
            ids.append(new_id)
        return ids

    async def apply_extractions(self, workspace_id: str, extraction_ids: list[str], *,
                                force: bool = False) -> dict[str, Any]:
        """Resolve and write extractions to the graph under the workspace lock."""
        totals: dict[str, Any] = {"entities": 0, "claims": 0, "evidence": 0, "contradictions": 0, "proposals": 0,
                                  "rejected": 0, "entity_ids": []}
        async with self.db.session() as s:
            await workspace_lock(s, workspace_id)
            registry = await load_registry(s, workspace_id)
            existing_entities = await self.graph.load_entities(workspace_id)
            existing_claims = await self.graph.load_relation_claims(workspace_id)
            resolver = OntologyResolver(workspace_id, registry, existing_entities, existing_claims)
            rows = (await s.execute(select(Extraction).where(
                Extraction.workspace_id == workspace_id, Extraction.id.in_(extraction_ids)))).scalars().all()
            for ex in rows:
                if ex.applied_at and not force:
                    continue
                batch = await self._resolve_one(s, resolver, ex)
                sources = await self._source_rows(s, workspace_id, {e.source_id for e in batch.evidence})
                counts = await self.graph.apply_batch(batch, sources)
                names = {eid: e.name for eid, e in resolver.entities.items()}
                await self._persist_side_effects(s, workspace_id, batch, names)
                report = {**counts, "proposals": len(batch.proposals), "rejected": batch.rejected[:50]}
                ex.applied_at = datetime.now(UTC)
                ex.apply_report = report
                for k in ("entities", "claims", "evidence", "contradictions"):
                    totals[k] += counts[k]
                totals["proposals"] += len(batch.proposals)
                totals["rejected"] += len(batch.rejected)
                totals["entity_ids"].extend(e.id for e in batch.entities)
                # later batches in this run must see claims written by earlier ones
                resolver.existing_claims.extend(
                    _as_existing(c) for c in batch.claims if c.kind == "relation")
        totals["entity_ids"] = list(dict.fromkeys(totals["entity_ids"]))
        return totals

    async def _resolve_one(self, s: AsyncSession, resolver: OntologyResolver, ex: Extraction) -> GraphWriteBatch:
        result = ExtractionResult.model_validate(ex.payload.get("result", {}))
        pid_map: dict[str, str] = ex.payload.get("passages", {})
        chunks = {c.id: c for c in (await s.execute(select(Chunk).where(Chunk.id.in_(list(pid_map.values())))
                                                    )).scalars().all()}
        passages = {pid: PassageRef(chunk_id=cid, source_id=chunks[cid].source_id, text=chunks[cid].text)
                    for pid, cid in pid_map.items() if cid in chunks}
        resolver._touched = set()
        return resolver.resolve(result, passages, extraction_id=ex.id, session_id=ex.session_id,
                                extracted_at=(ex.created_at or datetime.now(UTC)).isoformat())

    @staticmethod
    async def _source_rows(s: AsyncSession, workspace_id: str, source_ids: set[str]) -> list[dict[str, Any]]:
        if not source_ids:
            return []
        rows = (await s.execute(select(Source).where(Source.workspace_id == workspace_id,
                                                     Source.id.in_(list(source_ids))))).scalars().all()
        return [{"id": r.id, "title": r.title, "url": r.url, "published_at": r.published_at} for r in rows]

    @staticmethod
    async def _persist_side_effects(s: AsyncSession, workspace_id: str, batch: GraphWriteBatch,
                                    names: dict[str, str]) -> None:
        for p in batch.proposals:
            await s.execute(pg_insert(OntologyProposal).values(
                workspace_id=workspace_id, kind=p.kind, dedupe_key=p.dedupe_key, summary=p.summary,
                payload=p.payload).on_conflict_do_nothing(index_elements=["workspace_id", "dedupe_key"]))
            if p.kind == "new_relation_type":
                await s.execute(pg_insert(RelationType).values(
                    workspace_id=workspace_id, name=p.payload["name"], family=p.payload["family"],
                    status="proposed", synonyms=p.payload.get("examples", [])
                ).on_conflict_do_nothing(index_elements=["workspace_id", "name"]))
        # coverage: entities with claims in this batch are now "researched"
        now = datetime.now(UTC)
        researched = {c.subject_id for c in batch.claims} | {c.object_id for c in batch.claims if c.object_id}
        for eid in researched:
            if eid not in names:
                continue
            stmt = pg_insert(CoverageRecord).values(
                workspace_id=workspace_id, concept_key=eid, concept_name=names[eid][:300], researched_at=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=["workspace_id", "concept_key"],
                set_={"researched_at": stmt.excluded.researched_at, "concept_name": stmt.excluded.concept_name})
            await s.execute(stmt)

    async def rebuild_graph(self, workspace_id: str) -> dict[str, Any]:
        """Replay every stored extraction into the graph (no LLM calls)."""
        async with self.db.session() as s:
            ids = list((await s.execute(select(Extraction.id).where(Extraction.workspace_id == workspace_id)
                                        .order_by(Extraction.created_at))).scalars().all())
        return await self.apply_extractions(workspace_id, ids, force=True)

    async def mark_source_graph_built(self, workspace_id: str, source_id: str) -> None:
        async with self.db.session() as s:
            await s.execute(update(Source).where(Source.id == source_id, Source.workspace_id == workspace_id)
                            .values(graph_built_at=datetime.now(UTC), status="graph_built"))


def _as_existing(c):
    from app.services.knowledge.ontology import ExistingClaim

    return ExistingClaim(id=c.id, subject_id=c.subject_id, predicate=c.predicate_canonical, object_id=c.object_id,
                         negated=c.negated)
