"""Entity/relationship normalization, ontology validation, provenance and contradictions (pure, no I/O)."""

from __future__ import annotations

import pytest

from app.core.ids import stable_id
from app.services.knowledge.ontology import (
    ExistingClaim,
    ExistingEntity,
    OntologyResolver,
    PassageRef,
    entity_keys,
    split_alias,
    verify_quote,
)
from app.services.knowledge.relations import RelationRegistry, predicate_key
from app.services.knowledge.schemas import ExtractionResult

pytestmark = pytest.mark.nodb
WS = "ws-1"


@pytest.fixture
def reg() -> RelationRegistry:
    return RelationRegistry.default()


# ------------------------------------------------------------------ relation normalization


@pytest.mark.parametrize("raw,canonical,reversed_", [
    ("is represented using", "REPRESENTED_USING", False),       # quantum
    ("represented by", "REPRESENTED_USING", False),
    ("measures", "MEASURES", False),                            # IoT
    ("is measured by", "MEASURES", True),
    ("influenced", "INFLUENCED", False),                        # history
    ("was influenced by", "INFLUENCED", True),                  # passive voice of a known verb
    ("is a type of", "IS_A", False),                            # cloud
    ("consists of", "PART_OF", True),                           # inverse phrase
    ("followed", "PRECEDED", True),
    ("was developed by", "DEVELOPED_BY", False),
    ("invented", "DEVELOPED_BY", True),
    ("IS_A", "IS_A", False),                                     # canonical name itself
    ("leads to", "CAUSES", False),
])
def test_predicate_resolution(reg, raw, canonical, reversed_):
    r = reg.resolve(raw)
    assert (r.canonical, r.reversed) == (canonical, reversed_)
    assert r.status == "canonical"


def test_symmetric_relation_never_reversed(reg):
    r = reg.resolve("differs from")
    assert r.canonical == "CONTRASTS_WITH" and not r.reversed and r.symmetric


def test_unknown_predicate_becomes_valid_proposal(reg):
    r = reg.resolve("is encrypted with")
    assert r.status == "proposed" and r.matched_by == "new"
    assert r.canonical == "ENCRYPTED_WITH"


def test_proposed_name_is_sanitized(reg):
    r = reg.resolve("'; MATCH (n) DETACH DELETE n //")
    assert r.status == "proposed"
    assert all(ch.isupper() or ch.isdigit() or ch == "_" for ch in r.canonical)


def test_workspace_accepted_types_are_used(reg):
    reg.add_workspace_type("ENCRYPTED_WITH", "functional", ["is encrypted with", "is secured with"], "accepted")
    r = reg.resolve("is secured with")
    assert r.canonical == "ENCRYPTED_WITH" and r.status == "accepted"
    reg.add_workspace_type("bad name;", "functional", [], "accepted")  # rejected silently
    assert "bad name;" not in reg.specs


def test_predicate_key_ignores_aux_and_inflection():
    assert predicate_key("Measures") == predicate_key("measure") == predicate_key("is measuring")


# ------------------------------------------------------------------ entity normalization


def test_entity_keys_and_aliases():
    assert "qubit" in entity_keys("Qubits")
    assert "temperature sensor" in entity_keys("The temperature sensors")
    assert split_alias("Internet of Things (IoT)") == ("Internet of Things", ["IoT"])
    assert entity_keys("Mathematics")[0] == "mathematics"  # not singularized into nonsense


def _passages(texts: dict[str, str]) -> dict[str, PassageRef]:
    return {pid: PassageRef(chunk_id=f"chunk-{pid}", source_id=f"src-{pid}", text=t) for pid, t in texts.items()}


def _resolve(reg, data: dict, passages: dict[str, str], existing=None, claims=None):
    res = OntologyResolver(WS, reg, existing or [], claims or [])
    return res, res.resolve(ExtractionResult.model_validate(data), _passages(passages), extraction_id="ex1",
                            session_id="sess1", extracted_at="2026-01-01T00:00:00Z")


def test_quantum_relation_with_provenance(reg):
    _, batch = _resolve(reg, {
        "entities": [{"name": "Qubit", "category": "Concept"}, {"name": "Bloch sphere", "category": "Concept"}],
        "relations": [{"subject": "Qubits", "predicate": "is represented using", "object": "the Bloch sphere",
                       "passage": "P1", "quote": "A qubit is represented using the Bloch sphere", "confidence": 0.9}],
    }, {"P1": "Intro. A qubit is represented using the Bloch sphere. More text."})
    assert len(batch.entities) == 2
    (claim,) = batch.claims
    qubit = stable_id(WS, "entity", "qubit")
    assert claim.subject_id == qubit and claim.predicate_canonical == "REPRESENTED_USING"
    assert claim.predicate_raw == "is represented using"
    (ev,) = batch.evidence
    assert ev.quote_verified and ev.chunk_id == "chunk-P1" and ev.source_id == "src-P1"
    assert ev.extraction_id == "ex1" and ev.claim_id == claim.id


def test_iot_inverse_phrase_is_normalized_direction(reg):
    _, batch = _resolve(reg, {
        "entities": [{"name": "Temperature", "category": "Concept"},
                     {"name": "Temperature sensor", "category": "Technology"}],
        "relations": [{"subject": "Temperature", "predicate": "is measured by", "object": "Temperature sensor",
                       "passage": "P1", "quote": "temperature is measured by a temperature sensor"}],
    }, {"P1": "In buildings, temperature is measured by a temperature sensor."})
    (c,) = batch.claims
    assert c.predicate_canonical == "MEASURES"
    assert c.subject_id == stable_id(WS, "entity", "temperature sensor")
    assert c.object_id == stable_id(WS, "entity", "temperature")


def test_history_attribution_and_type_validation(reg):
    _, batch = _resolve(reg, {
        "entities": [{"name": "Lov Grover", "category": "Person"},
                     {"name": "Grover's algorithm", "category": "Method"}],
        # model emitted the direction backwards; DEVELOPED_BY needs a person/organization object
        "relations": [{"subject": "Lov Grover", "predicate": "was developed by",
                       "object": "Grover's algorithm", "passage": "P1", "quote": ""}],
    }, {"P1": "Grover's algorithm was developed by Lov Grover."})
    (c,) = batch.claims
    assert c.predicate_canonical == "DEVELOPED_BY" and c.direction_corrected
    assert c.object_id == stable_id(WS, "entity", "lov grover")
    assert not batch.evidence[0].quote_verified  # empty quote is never invented


def test_category_violation_without_valid_swap_is_flagged(reg):
    _, batch = _resolve(reg, {
        "entities": [{"name": "Photosynthesis", "category": "Concept"}, {"name": "Chlorophyll", "category": "Concept"}],
        "relations": [{"subject": "Photosynthesis", "predicate": "was invented by", "object": "Chlorophyll",
                       "passage": "P1"}]}, {"P1": "x" * 40})
    assert any(p.kind == "type_mismatch" for p in batch.proposals)


def test_cloud_acronym_alias_merges_with_existing(reg):
    existing = [ExistingEntity(id="e-iaas", name="Infrastructure as a Service", category="Concept",
                               keys=entity_keys("Infrastructure as a Service"))]
    res, batch = _resolve(reg, {
        "entities": [{"name": "IaaS", "category": "Concept", "aliases": ["Infrastructure as a Service"]}],
        "relations": [],
    }, {}, existing=existing)
    assert [e.id for e in batch.entities] == ["e-iaas"]
    assert "IaaS" in batch.entities[0].aliases
    assert res.key_index["iaas"] == "e-iaas"


def test_fuzzy_duplicate_creates_merge_proposal_not_silent_merge(reg):
    existing = [ExistingEntity(id="e1", name="Serverless computing", category="Concept",
                               keys=entity_keys("Serverless computing"))]
    _, batch = _resolve(reg, {"entities": [{"name": "Serverless computng", "category": "Concept"}]}, {},
                        existing=existing)
    new = [e for e in batch.entities if e.is_new]
    assert len(new) == 1 and new[0].id != "e1"
    assert any(p.kind == "entity_merge" for p in batch.proposals)


def test_homonym_kept_separate_by_category(reg):
    existing = [ExistingEntity(id="e-mercury", name="Mercury", category="Place", keys=["mercury"])]
    _, batch = _resolve(reg, {"entities": [{"name": "Mercury", "category": "Person"}]}, {}, existing=existing)
    (e,) = batch.entities
    assert e.id != "e-mercury" and e.category == "Person"
    assert any(p.kind == "homonym" for p in batch.proposals)


def test_self_loops_and_unknown_passages_are_rejected(reg):
    _, batch = _resolve(reg, {"relations": [
        {"subject": "Qubit", "predicate": "is a", "object": "Qubits", "passage": "P1"},
        {"subject": "A", "predicate": "uses", "object": "B", "passage": "P9"},
    ]}, {"P1": "x" * 50})
    assert not batch.claims
    reasons = {r["reason"] for r in batch.rejected}
    assert "self-referential relation" in reasons and any("unknown passage" in r for r in reasons)


def test_multiple_sources_keep_separate_evidence(reg):
    data = {"relations": [
        {"subject": "Enlightenment", "predicate": "influenced", "object": "French Revolution", "passage": "P1",
         "quote": "The Enlightenment influenced the French Revolution"},
        {"subject": "The Enlightenment", "predicate": "influenced", "object": "the French Revolution",
         "passage": "P2", "quote": "the Enlightenment influenced the French Revolution deeply"},
    ]}
    _, batch = _resolve(reg, data, {"P1": "The Enlightenment influenced the French Revolution.",
                                    "P2": "Many agree the Enlightenment influenced the French Revolution deeply."})
    assert len(batch.claims) == 1
    assert {e.source_id for e in batch.evidence} == {"src-P1", "src-P2"}


def test_negation_contradiction_is_preserved_not_overwritten(reg):
    data = {"relations": [
        {"subject": "Enlightenment", "predicate": "caused", "object": "French Revolution", "passage": "P1"},
        {"subject": "Enlightenment", "predicate": "caused", "object": "French Revolution", "passage": "P2",
         "negated": True, "perspective": "according to revisionist historians"},
    ]}
    _, batch = _resolve(reg, data, {"P1": "a" * 30, "P2": "b" * 30})
    assert len(batch.claims) == 2
    neg = next(c for c in batch.claims if c.negated)
    assert neg.perspective == "according to revisionist historians"
    (contra,) = batch.contradictions
    assert {contra.claim_a, contra.claim_b} == {c.id for c in batch.claims}
    assert any(p.kind == "contradiction" for p in batch.proposals)


def test_single_valued_relation_conflict_against_existing_claims(reg):
    bastille = stable_id(WS, "entity", "bastille")
    paris = stable_id(WS, "entity", "paris")
    existing_entities = [ExistingEntity(bastille, "Bastille", "Place", ["bastille"]),
                         ExistingEntity(paris, "Paris", "Place", ["paris"])]
    existing_claims = [ExistingClaim("c-old", bastille, "LOCATED_IN", paris, False)]
    _, batch = _resolve(reg, {"entities": [{"name": "Lyon", "category": "Place"}], "relations": [
        {"subject": "Bastille", "predicate": "is located in", "object": "Lyon", "passage": "P1"}]},
        {"P1": "c" * 30}, existing=existing_entities, claims=existing_claims)
    assert len(batch.contradictions) == 1 and "c-old" in (batch.contradictions[0].claim_a,
                                                         batch.contradictions[0].claim_b)


def test_new_relation_type_is_proposed(reg):
    _, batch = _resolve(reg, {"relations": [
        {"subject": "Sensor data", "predicate": "is encrypted with", "object": "AES", "passage": "P1"}]},
        {"P1": "Sensor data is encrypted with AES."})
    (c,) = batch.claims
    assert c.relation_status == "proposed" and c.predicate_canonical == "ENCRYPTED_WITH"
    assert [p.kind for p in batch.proposals] == ["new_relation_type"]


def test_ids_are_deterministic_for_idempotent_retries(reg):
    data = {"entities": [{"name": "MQTT", "category": "Method"}], "relations": [
        {"subject": "IoT device", "predicate": "uses", "object": "MQTT", "passage": "P1"}]}
    _, b1 = _resolve(reg, data, {"P1": "Each IoT device uses MQTT."})
    _, b2 = _resolve(reg, data, {"P1": "Each IoT device uses MQTT."})
    assert [c.id for c in b1.claims] == [c.id for c in b2.claims]
    assert [e.id for e in b1.evidence] == [e.id for e in b2.evidence]


def test_workspace_isolation_of_ids(reg):
    a = OntologyResolver("ws-a", reg, []).resolve_entity("Qubit")
    b = OntologyResolver("ws-b", reg, []).resolve_entity("Qubit")
    assert a.id != b.id


def test_verify_quote():
    assert verify_quote("Bloch   sphere represents", "The Bloch sphere represents a qubit.")
    assert verify_quote("made-up quotation text", "The Bloch sphere represents a qubit.") is None
    assert verify_quote("short", "short") is None
