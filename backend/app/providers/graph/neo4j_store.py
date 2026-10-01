"""Neo4j knowledge-graph store.

Safety rules enforced here:
- Only parameterized, hand-written Cypher. No LLM-generated queries.
- Node labels interpolated into queries come only from ``ENTITY_CATEGORIES``
  (validated allowlist). Semantic relation types are stored as the ``type``
  property of a single ``:REL`` relationship type, so dynamic ontology growth
  never requires dynamic Cypher.
- Every query is scoped by ``workspace_id``; all IDs embed the workspace.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired, TransientError

from app.core.errors import NotFoundError, ProviderError, ValidationFailed
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.services.knowledge.ontology import ExistingClaim, ExistingEntity
from app.services.knowledge.schemas import ENTITY_CATEGORIES, GraphWriteBatch

log = get_logger(__name__)

NODE_LABELS = ("Entity", "Source", "Claim", "Evidence", "ResearchSession", "TeachingMaterial")
CLAIM_STATUSES = ("unverified", "supported", "contested", "rejected", "teacher_verified")

SCHEMA_STATEMENTS = [
    *[f"CREATE CONSTRAINT {lbl.lower()}_id IF NOT EXISTS FOR (n:{lbl}) REQUIRE n.id IS UNIQUE" for lbl in NODE_LABELS],
    *[f"CREATE INDEX {lbl.lower()}_ws IF NOT EXISTS FOR (n:{lbl}) ON (n.workspace_id)" for lbl in NODE_LABELS],
    "CREATE INDEX rel_ws IF NOT EXISTS FOR ()-[r:REL]-() ON (r.workspace_id)",
    "CREATE INDEX rel_id IF NOT EXISTS FOR ()-[r:REL]-() ON (r.id)",
    "CREATE INDEX claim_ws_status IF NOT EXISTS FOR (n:Claim) ON (n.workspace_id, n.status)",
    "CREATE INDEX claim_subject IF NOT EXISTS FOR (n:Claim) ON (n.subject_id)",
]

_CATEGORY_REMOVE = ":".join(ENTITY_CATEGORIES)


def _label(category: str) -> str:
    if category not in ENTITY_CATEGORIES:
        raise ValidationFailed(f"Invalid entity category label: {category}")
    return category


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Neo4jGraphStore:
    def __init__(self, driver: AsyncDriver, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    @classmethod
    def connect(cls, uri: str, user: str, password: str, database: str = "neo4j") -> "Neo4jGraphStore":
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password), max_connection_lifetime=300,
                                           notifications_min_severity="OFF")
        return cls(driver, database)

    async def close(self) -> None:
        await self.driver.close()

    # ------------------------------------------------------------------ plumbing

    async def _write(self, fn) -> Any:
        try:
            async with self.driver.session(database=self.database) as session:
                return await session.execute_write(fn)
        except (ServiceUnavailable, SessionExpired, TransientError) as exc:
            raise ProviderError(f"Neo4j unavailable: {exc}", retryable=True) from exc
        except Neo4jError as exc:
            raise ProviderError(f"Neo4j error: {exc}", retryable=False) from exc

    async def _read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        try:
            async with self.driver.session(database=self.database) as session:
                async def fn(tx):
                    res = await tx.run(query, **params)
                    return await res.data()

                return await session.execute_read(fn)
        except (ServiceUnavailable, SessionExpired, TransientError) as exc:
            raise ProviderError(f"Neo4j unavailable: {exc}", retryable=True) from exc
        except Neo4jError as exc:
            raise ProviderError(f"Neo4j error: {exc}", retryable=False) from exc

    async def ensure_schema(self) -> None:
        # schema statements run in their own transactions
        for stmt in SCHEMA_STATEMENTS:
            async def one(tx, s=stmt):
                await (await tx.run(s)).consume()

            await self._write(one)
        log.info("neo4j_schema_ready", statements=len(SCHEMA_STATEMENTS))

    async def ping(self) -> bool:
        await self._read("RETURN 1 AS ok")
        return True

    # ------------------------------------------------------------------ writes

    async def apply_batch(self, batch: GraphWriteBatch, sources: list[dict[str, Any]]) -> dict[str, int]:
        ws = batch.workspace_id
        now = _now()
        by_cat: dict[str, list[dict]] = {}
        for e in batch.entities:
            by_cat.setdefault(_label(e.category), []).append(e.model_dump())
        claims = [c.model_dump() for c in batch.claims]
        evidence = [e.model_dump() for e in batch.evidence]
        contradictions = [c.model_dump() for c in batch.contradictions]
        touched_claims = list({c["id"] for c in claims} | {c["claim_a"] for c in contradictions}
                              | {c["claim_b"] for c in contradictions})
        session_ids = list({c["session_id"] for c in claims if c.get("session_id")})

        async def fn(tx):
            if sources:
                await (await tx.run(
                    """UNWIND $rows AS s
                       MERGE (n:Source {id: s.id})
                       ON CREATE SET n.workspace_id = $ws, n.created_at = $now
                       WITH n, s WHERE n.workspace_id = $ws
                       SET n.title = s.title, n.url = s.url, n.published_at = s.published_at""",
                    rows=sources, ws=ws, now=now)).consume()
            for cat, rows in by_cat.items():
                await (await tx.run(
                    f"""UNWIND $rows AS r
                        MERGE (e:Entity {{id: r.id}})
                        ON CREATE SET e.workspace_id = $ws, e.created_at = $now
                        WITH e, r WHERE e.workspace_id = $ws
                        REMOVE e:{_CATEGORY_REMOVE}
                        SET e:{cat}, e.name = r.name, e.category = r.category, e.keys = r.keys,
                            e.aliases = r.aliases, e.updated_at = $now,
                            e.description = CASE WHEN r.description <> '' THEN r.description
                                                 ELSE coalesce(e.description, '') END,
                            e.time_context = coalesce(r.time_context, e.time_context)""",
                    rows=rows, ws=ws, now=now)).consume()
            if session_ids:
                await (await tx.run(
                    """UNWIND $ids AS sid MERGE (s:ResearchSession {id: sid})
                       ON CREATE SET s.workspace_id = $ws, s.created_at = $now""",
                    ids=session_ids, ws=ws, now=now)).consume()
            if claims:
                await (await tx.run(
                    """UNWIND $rows AS c
                       MATCH (s:Entity {id: c.subject_id, workspace_id: $ws})
                       MERGE (cl:Claim {id: c.id})
                       ON CREATE SET cl.workspace_id = $ws, cl.created_at = $now, cl.status = 'unverified',
                                     cl.predicate_raw_forms = [], cl.perspectives = [], cl.session_ids = []
                       WITH cl, c, s WHERE cl.workspace_id = $ws
                       SET cl.text = c.text, cl.kind = c.kind, cl.subject_id = c.subject_id,
                           cl.object_id = c.object_id, cl.predicate = c.predicate_canonical, cl.family = c.family,
                           cl.negated = c.negated, cl.relation_status = c.relation_status,
                           cl.direction_corrected = c.direction_corrected,
                           cl.time_context = coalesce(c.time_context, cl.time_context),
                           cl.confidence = CASE WHEN cl.confidence IS NULL OR c.confidence > cl.confidence
                                                THEN c.confidence ELSE cl.confidence END,
                           cl.predicate_raw_forms = CASE WHEN c.predicate_raw IN cl.predicate_raw_forms
                                THEN cl.predicate_raw_forms ELSE cl.predicate_raw_forms + c.predicate_raw END,
                           cl.perspectives = CASE WHEN c.perspective IS NULL OR c.perspective IN cl.perspectives
                                THEN cl.perspectives ELSE cl.perspectives + c.perspective END,
                           cl.session_ids = CASE WHEN c.session_id IS NULL OR c.session_id IN cl.session_ids
                                THEN cl.session_ids ELSE cl.session_ids + c.session_id END
                       MERGE (cl)-[:ABOUT {role: 'subject'}]->(s)
                       WITH cl, c, s
                       OPTIONAL MATCH (o:Entity {id: c.object_id, workspace_id: $ws})
                       FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END |
                           MERGE (cl)-[:ABOUT {role: 'object'}]->(o))
                       FOREACH (_ IN CASE WHEN o IS NULL OR c.negated THEN [] ELSE [1] END |
                           MERGE (s)-[r:REL {id: c.id}]->(o)
                           SET r.workspace_id = $ws, r.type = c.predicate_canonical, r.family = c.family,
                               r.relation_status = c.relation_status, r.claim_id = c.id)
                       WITH cl, c
                       OPTIONAL MATCH (rs:ResearchSession {id: c.session_id, workspace_id: $ws})
                       FOREACH (_ IN CASE WHEN rs IS NULL THEN [] ELSE [1] END |
                           MERGE (cl)-[:DISCOVERED_IN]->(rs))""",
                    rows=claims, ws=ws, now=now)).consume()
            if evidence:
                await (await tx.run(
                    """UNWIND $rows AS ev
                       MATCH (cl:Claim {id: ev.claim_id, workspace_id: $ws})
                       MERGE (e:Evidence {id: ev.id})
                       ON CREATE SET e.workspace_id = $ws, e.quote_verified = false
                       WITH e, ev, cl WHERE e.workspace_id = $ws
                       SET e.chunk_id = ev.chunk_id, e.source_id = ev.source_id,
                           e.quote = CASE WHEN ev.quote_verified OR e.quote IS NULL THEN ev.quote ELSE e.quote END,
                           e.quote_verified = e.quote_verified OR ev.quote_verified,
                           e.confidence = ev.confidence, e.raw_predicate = ev.raw_predicate,
                           e.perspective = ev.perspective, e.extraction_id = ev.extraction_id,
                           e.extracted_at = ev.extracted_at
                       MERGE (cl)-[:SUPPORTED_BY]->(e)
                       WITH e, ev
                       MATCH (src:Source {id: ev.source_id, workspace_id: $ws})
                       MERGE (e)-[:FROM_SOURCE]->(src)""",
                    rows=evidence, ws=ws)).consume()
            if contradictions:
                await (await tx.run(
                    """UNWIND $rows AS c
                       MATCH (a:Claim {id: c.claim_a, workspace_id: $ws}), (b:Claim {id: c.claim_b, workspace_id: $ws})
                       MERGE (a)-[x:CONTRADICTS]->(b) SET x.reason = c.reason""",
                    rows=contradictions, ws=ws)).consume()
            if touched_claims:
                await self._refresh_claim_status(tx, ws, touched_claims)

        await self._write(fn)
        return {"entities": len(batch.entities), "claims": len(claims), "evidence": len(evidence),
                "contradictions": len(contradictions)}

    @staticmethod
    async def _refresh_claim_status(tx, ws: str, claim_ids: list[str]) -> None:
        await (await tx.run(
            """UNWIND $ids AS cid
               MATCH (cl:Claim {id: cid, workspace_id: $ws})
               OPTIONAL MATCH (cl)-[:SUPPORTED_BY]->(e:Evidence)
               WITH cl, count(e) AS n, sum(CASE WHEN e.quote_verified THEN 1 ELSE 0 END) AS nv,
                    collect(DISTINCT e.source_id) AS srcs
               OPTIONAL MATCH (cl)-[:CONTRADICTS]-(x:Claim) WHERE x.status <> 'rejected'
               WITH cl, n, nv, srcs, count(x) AS nc
               SET cl.evidence_count = n, cl.source_count = size(srcs),
                   cl.status = CASE WHEN cl.status IN ['rejected', 'teacher_verified'] THEN cl.status
                                    WHEN nc > 0 THEN 'contested'
                                    WHEN nv > 0 THEN 'supported' ELSE 'unverified' END
               WITH cl
               OPTIONAL MATCH ()-[r:REL {id: cl.id}]->()
               FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END |
                   SET r.claim_status = cl.status, r.evidence_count = cl.evidence_count,
                       r.source_count = cl.source_count)""",
            ids=claim_ids, ws=ws)).consume()

    async def set_claim_status(self, ws: str, claim_id: str, status: str, note: str = "") -> None:
        if status not in CLAIM_STATUSES:
            raise ValidationFailed("Invalid claim status")

        async def fn(tx):
            res = await tx.run(
                """MATCH (cl:Claim {id: $id, workspace_id: $ws})
                   SET cl.status = $status, cl.review_note = $note, cl.reviewed_at = $now
                   RETURN cl.id AS id""", id=claim_id, ws=ws, status=status, note=note, now=_now())
            rows = await res.data()
            if not rows:
                raise NotFoundError("Claim not found")
            if status not in ("rejected", "teacher_verified"):
                await tx.run("MATCH (cl:Claim {id: $id, workspace_id: $ws}) SET cl.status = 'unverified'",
                             id=claim_id, ws=ws)
                await Neo4jGraphStore._refresh_claim_status(tx, ws, [claim_id])
            else:
                await (await tx.run(
                    """MATCH ()-[r:REL {id: $id}]->() WHERE r.workspace_id = $ws SET r.claim_status = $status""",
                    id=claim_id, ws=ws, status=status)).consume()
            # contested status of counterpart claims may change when one side is rejected
            others = await (await tx.run(
                "MATCH (:Claim {id: $id, workspace_id: $ws})-[:CONTRADICTS]-(x:Claim) RETURN x.id AS id",
                id=claim_id, ws=ws)).data()
            if others:
                await Neo4jGraphStore._refresh_claim_status(tx, ws, [o["id"] for o in others])

        await self._write(fn)

    async def remap_relation_type(self, ws: str, from_type: str, to_type: str, family: str, status: str) -> int:
        async def fn(tx):
            res = await tx.run(
                """MATCH ()-[r:REL {type: $from}]->() WHERE r.workspace_id = $ws
                   SET r.type = $to, r.family = $family, r.relation_status = $status
                   WITH r MATCH (cl:Claim {id: r.id, workspace_id: $ws})
                   SET cl.predicate = $to, cl.family = $family, cl.relation_status = $status
                   RETURN count(cl) AS n""", ws=ws, **{"from": from_type}, to=to_type, family=family, status=status)
            return (await res.single())["n"]

        return await self._write(fn)

    async def merge_entities(self, ws: str, keep_id: str, dup_id: str) -> dict[str, int]:
        if keep_id == dup_id:
            raise ValidationFailed("Cannot merge an entity into itself")

        async def fn(tx):
            found = await (await tx.run(
                "MATCH (e:Entity {workspace_id: $ws}) WHERE e.id IN [$k, $d] RETURN count(e) AS n",
                ws=ws, k=keep_id, d=dup_id)).single()
            if found["n"] != 2:
                raise NotFoundError("Entity not found")
            await (await tx.run(
                """MATCH (keep:Entity {id: $k, workspace_id: $ws}), (dup:Entity {id: $d, workspace_id: $ws})
                   CALL (keep, dup) {
                     MATCH (dup)-[r:REL]->(o) WHERE o <> keep
                     MERGE (keep)-[n:REL {id: r.id}]->(o) SET n += properties(r) DELETE r
                   }
                   CALL (keep, dup) {
                     MATCH (o)-[r:REL]->(dup) WHERE o <> keep
                     MERGE (o)-[n:REL {id: r.id}]->(keep) SET n += properties(r) DELETE r
                   }
                   CALL (keep, dup) {
                     MATCH (c:Claim)-[a:ABOUT]->(dup)
                     MERGE (c)-[:ABOUT {role: a.role}]->(keep) DELETE a
                   }
                   CALL (keep, dup) {
                     MATCH (t:TeachingMaterial)-[a:COVERS]->(dup) MERGE (t)-[:COVERS]->(keep) DELETE a
                   }
                   SET keep.keys = keep.keys + [k IN dup.keys WHERE NOT k IN keep.keys],
                       keep.aliases = keep.aliases + [a IN dup.aliases + [dup.name] WHERE NOT a IN keep.aliases
                                                      AND a <> keep.name],
                       keep.description = CASE WHEN coalesce(keep.description, '') = '' THEN dup.description
                                               ELSE keep.description END
                   WITH keep, dup
                   MATCH (c:Claim {workspace_id: $ws}) WHERE c.subject_id = $d OR c.object_id = $d
                   SET c.subject_id = CASE WHEN c.subject_id = $d THEN $k ELSE c.subject_id END,
                       c.object_id = CASE WHEN c.object_id = $d THEN $k ELSE c.object_id END
                   WITH DISTINCT keep, dup
                   DETACH DELETE dup""", ws=ws, k=keep_id, d=dup_id)).consume()
            # self-loops can appear when both endpoints collapse into one entity
            await (await tx.run(
                "MATCH (e:Entity {id: $k, workspace_id: $ws})-[r:REL]->(e) DELETE r", k=keep_id, ws=ws)).consume()
            # dedupe claims that became identical; keep the ID future extractions will generate
            rows = await (await tx.run(
                """MATCH (c:Claim {workspace_id: $ws}) WHERE c.subject_id = $k OR c.object_id = $k
                   RETURN c.id AS id, c.kind AS kind, c.subject_id AS s, c.predicate AS p, c.object_id AS o,
                          c.negated AS neg, c.text AS text""", ws=ws, k=keep_id)).data()
            groups: dict[tuple, list[dict]] = {}
            for r in rows:
                if r["kind"] == "relation":
                    canonical = stable_id(ws, "claim", r["s"], r["p"], r["o"], bool(r["neg"]))
                    groups.setdefault((r["s"], r["p"], r["o"], bool(r["neg"]), canonical), []).append(r)
            merged = 0
            for key, members in groups.items():
                canonical = key[4]
                ids = [m["id"] for m in members]
                if len(ids) == 1 and ids[0] == canonical:
                    continue
                keep_claim = canonical if canonical in ids else ids[0]
                for other in ids:
                    if other == keep_claim:
                        continue
                    await (await tx.run(
                        """MATCH (a:Claim {id: $keep, workspace_id: $ws}), (b:Claim {id: $other, workspace_id: $ws})
                           CALL (a, b) { MATCH (b)-[r:SUPPORTED_BY]->(e) MERGE (a)-[:SUPPORTED_BY]->(e) DELETE r }
                           CALL (a, b) { MATCH (b)-[r:CONTRADICTS]-(x) WHERE x <> a MERGE (a)-[:CONTRADICTS]->(x) DELETE r }
                           CALL (a, b) { MATCH (b)-[r:DISCOVERED_IN]->(x) MERGE (a)-[:DISCOVERED_IN]->(x) DELETE r }
                           WITH a, b
                           OPTIONAL MATCH ()-[rel:REL {id: b.id}]->() WHERE rel.workspace_id = $ws
                           DELETE rel
                           WITH DISTINCT a, b
                           DETACH DELETE b""", keep=keep_claim, other=other, ws=ws)).consume()
                    merged += 1
                if keep_claim != canonical:
                    await (await tx.run(
                        """MATCH (a:Claim {id: $old, workspace_id: $ws}) SET a.id = $new
                           WITH a OPTIONAL MATCH ()-[r:REL {id: $old}]->() WHERE r.workspace_id = $ws
                           FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END | SET r.id = $new, r.claim_id = $new)
                        """, old=keep_claim, new=canonical, ws=ws)).consume()
            ids = [r["id"] for r in rows]
            if ids:
                current = await (await tx.run(
                    """MATCH (c:Claim {workspace_id: $ws}) WHERE c.subject_id = $k OR c.object_id = $k
                       RETURN collect(c.id) AS ids""", ws=ws, k=keep_id)).single()
                await self._refresh_claim_status(tx, ws, current["ids"])
            return {"claims_merged": merged}

        return await self._write(fn)

    async def link_teaching_material(self, ws: str, material_id: str, title: str, kind: str,
                                     entity_ids: list[str]) -> None:
        async def fn(tx):
            await (await tx.run(
                """MERGE (t:TeachingMaterial {id: $id})
                   ON CREATE SET t.workspace_id = $ws, t.created_at = $now
                   WITH t WHERE t.workspace_id = $ws
                   SET t.title = $title, t.kind = $kind
                   WITH t
                   OPTIONAL MATCH (t)-[old:COVERS]->() DELETE old
                   WITH DISTINCT t
                   UNWIND $eids AS eid
                   MATCH (e:Entity {id: eid, workspace_id: $ws})
                   MERGE (t)-[:COVERS]->(e)""",
                id=material_id, ws=ws, title=title, kind=kind, eids=entity_ids, now=_now())).consume()

        await self._write(fn)

    async def delete_workspace(self, ws: str) -> None:
        async def fn(tx):
            await (await tx.run("MATCH (n {workspace_id: $ws}) DETACH DELETE n", ws=ws)).consume()

        await self._write(fn)

    # ------------------------------------------------------------------ reads

    async def load_entities(self, ws: str) -> list[ExistingEntity]:
        rows = await self._read(
            """MATCH (e:Entity {workspace_id: $ws})
               RETURN e.id AS id, e.name AS name, e.category AS category, e.keys AS keys, e.aliases AS aliases""",
            ws=ws)
        return [ExistingEntity(id=r["id"], name=r["name"], category=r["category"], keys=r["keys"] or [],
                               aliases=r["aliases"] or []) for r in rows]

    async def load_relation_claims(self, ws: str) -> list[ExistingClaim]:
        rows = await self._read(
            """MATCH (c:Claim {workspace_id: $ws, kind: 'relation'}) WHERE c.status <> 'rejected'
               RETURN c.id AS id, c.subject_id AS s, c.predicate AS p, c.object_id AS o, c.negated AS neg""", ws=ws)
        return [ExistingClaim(id=r["id"], subject_id=r["s"], predicate=r["p"], object_id=r["o"],
                              negated=bool(r["neg"])) for r in rows]

    async def stats(self, ws: str) -> dict[str, int]:
        rows = await self._read(
            """CALL () { MATCH (e:Entity {workspace_id: $ws}) RETURN count(e) AS entities }
               CALL () { MATCH (c:Claim {workspace_id: $ws}) RETURN count(c) AS claims }
               CALL () { MATCH (c:Claim {workspace_id: $ws, status: 'contested'}) RETURN count(c) AS contested }
               CALL () { MATCH (c:Claim {workspace_id: $ws, status: 'unverified'}) RETURN count(c) AS unverified }
               CALL () { MATCH ()-[r:REL {workspace_id: $ws}]->() RETURN count(r) AS relations }
               CALL () { MATCH (s:Source {workspace_id: $ws}) RETURN count(s) AS sources }
               RETURN entities, claims, contested, unverified, relations, sources""", ws=ws)
        return rows[0] if rows else {}

    async def overview(self, ws: str, limit: int = 150) -> dict[str, Any]:
        nodes = await self._read(
            """MATCH (e:Entity {workspace_id: $ws})
               OPTIONAL MATCH (e)-[r:REL]-()
               WITH e, count(r) AS degree ORDER BY degree DESC, e.name LIMIT $limit
               RETURN e.id AS id, e.name AS name, e.category AS category, e.description AS description,
                      degree""", ws=ws, limit=limit)
        ids = [n["id"] for n in nodes]
        edges = await self._read(
            """MATCH (a:Entity)-[r:REL {workspace_id: $ws}]->(b:Entity)
               WHERE a.id IN $ids AND b.id IN $ids
               RETURN r.id AS id, a.id AS source, b.id AS target, r.type AS type, r.family AS family,
                      r.claim_status AS status, coalesce(r.evidence_count, 0) AS evidence_count""",
            ws=ws, ids=ids)
        return {"nodes": nodes, "edges": edges}

    async def neighborhood(self, ws: str, entity_id: str, depth: int = 1, limit: int = 80) -> dict[str, Any]:
        depth = 2 if depth >= 2 else 1
        nodes = await self._read(
            f"""MATCH (c:Entity {{id: $id, workspace_id: $ws}})
                OPTIONAL MATCH (c)-[:REL*1..{depth}]-(n:Entity {{workspace_id: $ws}})
                WITH c, collect(DISTINCT n)[..$limit] AS ns
                UNWIND [c] + ns AS e
                RETURN DISTINCT e.id AS id, e.name AS name, e.category AS category, e.description AS description""",
            ws=ws, id=entity_id, limit=limit)
        if not nodes:
            raise NotFoundError("Entity not found")
        ids = [n["id"] for n in nodes]
        edges = await self._read(
            """MATCH (a:Entity)-[r:REL {workspace_id: $ws}]->(b:Entity) WHERE a.id IN $ids AND b.id IN $ids
               RETURN r.id AS id, a.id AS source, b.id AS target, r.type AS type, r.family AS family,
                      r.claim_status AS status, coalesce(r.evidence_count, 0) AS evidence_count""", ws=ws, ids=ids)
        return {"nodes": nodes, "edges": edges}

    async def search_entities(self, ws: str, q: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self._read(
            """MATCH (e:Entity {workspace_id: $ws})
               WHERE toLower(e.name) CONTAINS toLower($q) OR any(a IN e.aliases WHERE toLower(a) CONTAINS toLower($q))
               RETURN e.id AS id, e.name AS name, e.category AS category, e.description AS description
               ORDER BY size(e.name) LIMIT $limit""", ws=ws, q=q, limit=limit)

    async def entity_detail(self, ws: str, entity_id: str) -> dict[str, Any]:
        """Entity with every claim about it and the evidence/sources behind each (why it was added)."""
        ent = await self._read(
            """MATCH (e:Entity {id: $id, workspace_id: $ws})
               RETURN e.id AS id, e.name AS name, e.category AS category, e.description AS description,
                      e.aliases AS aliases, e.time_context AS time_context, e.created_at AS created_at""",
            ws=ws, id=entity_id)
        if not ent:
            raise NotFoundError("Entity not found")
        claims = await self._claims_with_evidence(ws, "MATCH (cl:Claim {workspace_id: $ws})-[:ABOUT]->"
                                                      "(:Entity {id: $id})", id=entity_id)
        return {**ent[0], "claims": claims}

    async def claim_detail(self, ws: str, claim_id: str) -> dict[str, Any]:
        claims = await self._claims_with_evidence(ws, "MATCH (cl:Claim {id: $id, workspace_id: $ws})", id=claim_id)
        if not claims:
            raise NotFoundError("Claim not found")
        return claims[0]

    async def _claims_with_evidence(self, ws: str, match: str, **params: Any) -> list[dict[str, Any]]:
        return await self._read(
            f"""{match}
                WITH DISTINCT cl
                OPTIONAL MATCH (cl)-[:SUPPORTED_BY]->(ev:Evidence)
                OPTIONAL MATCH (ev)-[:FROM_SOURCE]->(src:Source)
                OPTIONAL MATCH (cl)-[:CONTRADICTS]-(x:Claim)
                WITH cl, collect(DISTINCT {{evidence_id: ev.id, chunk_id: ev.chunk_id, quote: ev.quote,
                                           quote_verified: ev.quote_verified, confidence: ev.confidence,
                                           raw_predicate: ev.raw_predicate, perspective: ev.perspective,
                                           extracted_at: ev.extracted_at, source_id: src.id,
                                           source_title: src.title, source_url: src.url}}) AS evidence,
                     collect(DISTINCT {{id: x.id, text: x.text, status: x.status}}) AS contradicts
                RETURN cl.id AS id, cl.text AS text, cl.kind AS kind, cl.predicate AS predicate,
                       cl.predicate_raw_forms AS raw_predicates, cl.family AS family, cl.subject_id AS subject_id,
                       cl.object_id AS object_id, cl.negated AS negated, cl.status AS status,
                       cl.relation_status AS relation_status, cl.confidence AS confidence,
                       cl.time_context AS time_context, cl.perspectives AS perspectives,
                       cl.session_ids AS session_ids, cl.created_at AS created_at,
                       [e IN evidence WHERE e.evidence_id IS NOT NULL] AS evidence,
                       [c IN contradicts WHERE c.id IS NOT NULL] AS contradicts
                ORDER BY cl.created_at""", ws=ws, **params)

    async def list_claims(self, ws: str, status: str | None = None, limit: int = 100,
                          offset: int = 0) -> list[dict[str, Any]]:
        return await self._read(
            """MATCH (cl:Claim {workspace_id: $ws}) WHERE $status IS NULL OR cl.status = $status
               RETURN cl.id AS id, cl.text AS text, cl.kind AS kind, cl.predicate AS predicate, cl.family AS family,
                      cl.status AS status, cl.confidence AS confidence, coalesce(cl.evidence_count, 0) AS evidence_count,
                      coalesce(cl.source_count, 0) AS source_count, cl.subject_id AS subject_id,
                      cl.object_id AS object_id
               ORDER BY cl.created_at DESC SKIP $offset LIMIT $limit""",
            ws=ws, status=status, limit=limit, offset=offset)

    async def concept_outline(self, ws: str, limit: int = 120) -> dict[str, Any]:
        """Compact graph view for planning: entities ranked by connectivity plus their relations."""
        nodes = await self._read(
            """MATCH (e:Entity {workspace_id: $ws})
               OPTIONAL MATCH (e)-[r:REL]-()
               OPTIONAL MATCH (cl:Claim)-[:ABOUT]->(e)
               WITH e, count(DISTINCT r) AS degree, count(DISTINCT cl) AS claims,
                    sum(DISTINCT coalesce(cl.source_count, 0)) AS src
               ORDER BY degree + claims DESC, e.name LIMIT $limit
               RETURN e.id AS id, e.name AS name, e.category AS category, e.description AS description,
                      e.time_context AS time_context, degree, claims, src AS source_support""",
            ws=ws, limit=limit)
        ids = [n["id"] for n in nodes]
        edges = await self._read(
            """MATCH (a:Entity)-[r:REL {workspace_id: $ws}]->(b:Entity) WHERE a.id IN $ids AND b.id IN $ids
                 AND coalesce(r.claim_status, 'unverified') <> 'rejected'
               RETURN a.id AS source, b.id AS target, r.type AS type, r.family AS family,
                      r.claim_status AS status""", ws=ws, ids=ids)
        return {"nodes": nodes, "edges": edges}

    async def facts_for_entities(self, ws: str, entity_ids: list[str], limit: int = 40) -> list[dict[str, Any]]:
        if not entity_ids:
            return []
        return await self._read(
            """MATCH (cl:Claim {workspace_id: $ws})-[:ABOUT]->(e:Entity) WHERE e.id IN $ids
                 AND cl.status <> 'rejected'
               WITH DISTINCT cl
               OPTIONAL MATCH (cl)-[:SUPPORTED_BY]->(ev:Evidence)
               WITH cl, collect(DISTINCT ev.chunk_id)[..3] AS chunk_ids, collect(DISTINCT ev.source_id)[..3] AS source_ids
               RETURN cl.id AS id, cl.text AS text, cl.status AS status, cl.kind AS kind,
                      cl.perspectives AS perspectives, cl.time_context AS time_context, chunk_ids, source_ids
               ORDER BY CASE cl.status WHEN 'teacher_verified' THEN 0 WHEN 'supported' THEN 1
                                       WHEN 'contested' THEN 2 ELSE 3 END, coalesce(cl.source_count, 0) DESC
               LIMIT $limit""", ws=ws, ids=entity_ids, limit=limit)

    async def entities_by_ids(self, ws: str, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        return await self._read(
            """MATCH (e:Entity {workspace_id: $ws}) WHERE e.id IN $ids
               RETURN e.id AS id, e.name AS name, e.category AS category, e.description AS description""",
            ws=ws, ids=ids)

    async def claim_ids_for_chunks(self, ws: str, chunk_ids: list[str]) -> list[str]:
        rows = await self._read(
            """MATCH (cl:Claim {workspace_id: $ws})-[:SUPPORTED_BY]->(ev:Evidence) WHERE ev.chunk_id IN $ids
               RETURN DISTINCT cl.id AS id""", ws=ws, ids=chunk_ids)
        return [r["id"] for r in rows]

    async def entities_for_chunks(self, ws: str, chunk_ids: list[str]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        return await self._read(
            """MATCH (e:Entity {workspace_id: $ws})<-[:ABOUT]-(cl:Claim)-[:SUPPORTED_BY]->(ev:Evidence)
               WHERE ev.chunk_id IN $ids
               RETURN e.id AS id, e.name AS name, count(DISTINCT cl) AS weight ORDER BY weight DESC LIMIT 30""",
            ws=ws, ids=chunk_ids)

    async def evidence_chunks_for_entities(self, ws: str, entity_ids: list[str], limit: int = 200) -> list[str]:
        if not entity_ids:
            return []
        rows = await self._read(
            """MATCH (e:Entity {workspace_id: $ws})<-[:ABOUT]-(cl:Claim)-[:SUPPORTED_BY]->(ev:Evidence)
               WHERE e.id IN $ids AND cl.status <> 'rejected'
               RETURN DISTINCT ev.chunk_id AS chunk_id LIMIT $limit""", ws=ws, ids=entity_ids, limit=limit)
        return [r["chunk_id"] for r in rows]
