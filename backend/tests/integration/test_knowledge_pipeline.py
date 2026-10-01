"""Knowledge pipeline against real PostgreSQL + Neo4j (+ in-memory Qdrant, local BGE).

Runs the same domain-agnostic pipeline over four unrelated fields to make sure the
architecture is not tuned to one subject. The FakeLLM's rule-based extraction stands in
for Groq; these tests validate normalization, provenance, persistence and isolation,
not extraction quality.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.container import Container
from app.core.errors import NotFoundError
from app.db.models import CoverageRecord, Extraction, OntologyProposal, Source, Workspace
from app.providers.search.fixture import FixtureSearchProvider

pytestmark = pytest.mark.integration


async def _workspace(c: Container, topic: str, owner: str = "teacher-a") -> str:
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import User

    async with c.db.session() as s:
        await s.execute(insert(User).values(id=owner).on_conflict_do_nothing())
        ws = Workspace(owner_id=owner, title=topic, topic=topic)
        s.add(ws)
        await s.flush()
        return ws.id


async def _ingest_topic(c: Container, ws_id: str, query: str) -> list[str]:
    outcome = await c.research.research(ws_id, [query])
    for sid in outcome.new_source_ids:
        ex = await c.knowledge.extract_source(ws_id, sid, topic=query, session_id=None)
        await c.knowledge.apply_extractions(ws_id, ex)
        await c.knowledge.mark_source_graph_built(ws_id, sid)
    return outcome.new_source_ids


@pytest.mark.parametrize("query,expect_entity,expect_rel", [
    ("quantum computing qubits", "Qubit", "REPRESENTED_USING"),
    ("cloud computing serverless lambda", "AWS Lambda", "IS_A"),
    ("iot sensors mqtt", "temperature sensor", "MEASURES"),
    ("french revolution enlightenment history", "French Revolution", "CAUSES"),
])
async def test_domain_agnostic_pipeline(container: Container, query, expect_entity, expect_rel):
    c = container
    ws = await _workspace(c, query)
    sources = await _ingest_topic(c, ws, query)
    assert len(sources) == 2
    stats = await c.graph.stats(ws)
    assert stats["entities"] >= 5 and stats["claims"] >= 4 and stats["sources"] == 2
    hits = await c.graph.search_entities(ws, expect_entity)
    assert hits, f"{expect_entity} not in graph"
    detail = await c.graph.entity_detail(ws, hits[0]["id"])
    assert detail["claims"], "entity has no claims"
    rel_claims = [cl for cl in detail["claims"] if cl["predicate"] == expect_rel]
    assert rel_claims, [cl["predicate"] for cl in detail["claims"]]
    ev = rel_claims[0]["evidence"][0]
    # provenance: every claim traces to a chunk, a source title/URL and the raw predicate
    assert ev["chunk_id"] and ev["source_url"].startswith("https://") and ev["raw_predicate"]
    assert ev["quote_verified"] is True
    vectors = await c.vectors.count(ws)
    assert vectors >= 2
    async with c.db.session() as s:
        researched = (await s.execute(select(func.count()).select_from(CoverageRecord).where(
            CoverageRecord.workspace_id == ws, CoverageRecord.researched_at.is_not(None)))).scalar_one()
    assert researched >= 3


async def test_multi_source_evidence_and_contradictions(container: Container):
    c = container
    ws = await _workspace(c, "history")
    await _ingest_topic(c, ws, "french revolution enlightenment history")
    contested = await c.graph.list_claims(ws, status="contested")
    texts = {cl["text"] for cl in contested}
    # assertion vs. explicit denial are both kept and marked contested (not overwritten)
    assert any("does NOT causes" in t or "does NOT" in t for t in texts), texts
    assert any("Enlightenment causes French Revolution" in t for t in texts), texts
    negated = next(cl for cl in contested if "NOT" in cl["text"])
    detail = await c.graph.claim_detail(ws, negated["id"])
    assert detail["contradicts"] and detail["evidence"][0]["perspective"] == "according to revisionist historians"
    # single-valued relation conflict (Bastille located in two places) becomes a review proposal
    async with c.db.session() as s:
        kinds = (await s.execute(select(OntologyProposal.kind).where(OntologyProposal.workspace_id == ws))
                 ).scalars().all()
    assert kinds.count("contradiction") >= 2
    # the same event ordering stated by two sources (preceded / followed) -> one claim, two sources
    rows = await c.graph.list_claims(ws, limit=200)
    preceded = [r for r in rows if r["predicate"] == "PRECEDED"]
    assert preceded and max(r["source_count"] for r in preceded) == 2


async def test_idempotent_reapply_and_rebuild(container: Container, fake_llm):
    c = container
    ws = await _workspace(c, "iot")
    await _ingest_topic(c, ws, "iot sensors mqtt")
    before = await c.graph.stats(ws)
    calls_before = sum(1 for call in fake_llm.calls if call["task"] == "extract")
    # retrying extraction for the same sources must not call the LLM again nor duplicate anything
    async with c.db.session() as s:
        sids = (await s.execute(select(Source.id).where(Source.workspace_id == ws))).scalars().all()
    for sid in sids:
        ex = await c.knowledge.extract_source(ws, sid, topic="iot", session_id=None)
        await c.knowledge.apply_extractions(ws, ex, force=True)
    assert sum(1 for call in fake_llm.calls if call["task"] == "extract") == calls_before
    assert await c.graph.stats(ws) == before
    # rebuilding the graph from PostgreSQL alone reproduces it
    await c.graph.delete_workspace(ws)
    assert (await c.graph.stats(ws))["entities"] == 0
    await c.knowledge.rebuild_graph(ws)
    after = await c.graph.stats(ws)
    assert after["entities"] == before["entities"] and after["claims"] == before["claims"]
    async with c.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(Extraction).where(
            Extraction.workspace_id == ws))).scalar_one() >= 2


async def test_new_relation_type_proposal_and_remap(container: Container):
    c = container
    ws = await _workspace(c, "iot")
    await _ingest_topic(c, ws, "iot sensors mqtt edge")
    async with c.db.session() as s:
        prop = (await s.execute(select(OntologyProposal).where(
            OntologyProposal.workspace_id == ws, OntologyProposal.kind == "new_relation_type"))).scalars().first()
    assert prop is not None and prop.payload["name"] == "ENCRYPTED_WITH"
    changed = await c.graph.remap_relation_type(ws, "ENCRYPTED_WITH", "USES", "functional", "canonical")
    assert changed >= 1
    rows = await c.graph.list_claims(ws, limit=200)
    assert not any(r["predicate"] == "ENCRYPTED_WITH" for r in rows)


async def test_entity_merge_rewires_relations_and_dedupes_claims(container: Container):
    c = container
    ws = await _workspace(c, "cloud")
    await _ingest_topic(c, ws, "cloud computing serverless lambda")
    lam = (await c.graph.search_entities(ws, "AWS Lambda"))[0]
    svc = (await c.graph.search_entities(ws, "serverless compute service"))[0]
    before = await c.graph.entity_detail(ws, lam["id"])
    await c.graph.merge_entities(ws, lam["id"], svc["id"])
    after = await c.graph.entity_detail(ws, lam["id"])
    assert "serverless compute service" in [a.lower() for a in after["aliases"]]
    assert len(after["claims"]) >= len(before["claims"]) - 1
    with pytest.raises(NotFoundError):
        await c.graph.entity_detail(ws, svc["id"])


async def test_graph_workspace_isolation(container: Container):
    c = container
    ws_a = await _workspace(c, "quantum", "teacher-a")
    ws_b = await _workspace(c, "quantum", "teacher-b")
    await _ingest_topic(c, ws_a, "quantum computing qubits")
    assert (await c.graph.stats(ws_b))["entities"] == 0
    assert await c.graph.search_entities(ws_b, "Qubit") == []
    a_entity = (await c.graph.search_entities(ws_a, "Qubit"))[0]
    with pytest.raises(NotFoundError):
        await c.graph.entity_detail(ws_b, a_entity["id"])  # other workspace's id is not reachable
    assert await c.retrieval.search(ws_b, "qubit bloch sphere") == []
    assert await c.vectors.count(ws_b) == 0
    # same research in B creates distinct nodes, never merged with A's private research
    await _ingest_topic(c, ws_b, "quantum computing qubits")
    b_entity = (await c.graph.search_entities(ws_b, "Qubit"))[0]
    assert b_entity["id"] != a_entity["id"]


async def test_search_cache_and_source_dedup(container: Container):
    c = container
    ws = await _workspace(c, "quantum")
    search: FixtureSearchProvider = c.search  # type: ignore[assignment]
    first = await c.research.research(ws, ["quantum computing qubits"])
    n_calls = len(search.calls)
    second = await c.research.research(ws, ["Quantum computing   QUBITS"])  # normalized -> cache hit
    assert len(search.calls) == n_calls and second.cached_queries
    assert not second.new_source_ids and set(second.reused_source_ids) == set(first.new_source_ids)
    async with c.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(Source).where(Source.workspace_id == ws))
                ).scalar_one() == 2
