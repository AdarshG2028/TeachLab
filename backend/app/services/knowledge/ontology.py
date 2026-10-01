"""Ontology resolver: turns untrusted LLM extraction candidates into validated graph writes.

The LLM *proposes*; this module *decides*. It is pure (no I/O) so it can be
tested deterministically across domains. Responsibilities:

- entity normalization, alias/acronym handling and matching against existing entities
- duplicate detection (exact keys auto-merge; fuzzy/acronym matches become review proposals)
- homonym separation when categories are incompatible
- predicate normalization to canonical relation types (synonyms, inverse phrases, passive voice)
- direction and entity-category validation
- new relation-type proposals
- claim/evidence construction with quote verification (quotes not found in the passage are dropped)
- contradiction detection (negation conflicts, single-valued relations with differing objects)
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from app.core.ids import normalize_key, stable_id
from app.services.knowledge.relations import RelationRegistry, ResolvedPredicate
from app.services.knowledge.schemas import (
    ClaimWrite,
    ContradictionWrite,
    EntityWrite,
    EvidenceWrite,
    ExtractionResult,
    GraphWriteBatch,
    ProposalWrite,
)

ABSTRACT = {"Concept", "Technology", "Method", "Other"}
_PAREN = re.compile(r"^(?P<name>.+?)\s*\((?P<alias>[^()]{1,40})\)\s*$")
_NON_PLURAL_ENDINGS = ("ss", "us", "is", "ics", "sis", "ous", "as")


@dataclass
class ExistingEntity:
    id: str
    name: str
    category: str
    keys: list[str]
    aliases: list[str] = field(default_factory=list)


@dataclass
class ExistingClaim:
    id: str
    subject_id: str
    predicate: str
    object_id: str | None
    negated: bool


@dataclass
class PassageRef:
    chunk_id: str
    source_id: str
    text: str


def singularize(word: str) -> str:
    if len(word) <= 3 or word.endswith(_NON_PLURAL_ENDINGS):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "zes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def entity_keys(name: str) -> list[str]:
    base = normalize_key(name)
    if not base:
        return []
    words = base.split(" ")
    singular = " ".join([*words[:-1], singularize(words[-1])])
    no_article = re.sub(r"^(the|a|an) ", "", singular)
    return list(dict.fromkeys(k for k in (base, singular, no_article) if k))


def split_alias(name: str) -> tuple[str, list[str]]:
    """'Internet of Things (IoT)' -> ('Internet of Things', ['IoT'])."""
    m = _PAREN.match(name)
    if m and not m.group("name").endswith(" "):
        return m.group("name").strip(), [m.group("alias").strip()]
    return name, []


def acronym(key: str) -> str:
    words = [w for w in key.split(" ") if w not in {"of", "the", "and", "for", "in", "on", "to", "a", "an"}]
    return "".join(w[0] for w in words if w) if len(words) >= 2 else ""


def compatible(a: str, b: str) -> bool:
    return a == b or (a in ABSTRACT and b in ABSTRACT)


def _norm_ws(text: str) -> str:
    return " ".join(text.split()).casefold()


def verify_quote(quote: str, passage: str) -> str | None:
    """Return the quote if it appears verbatim (whitespace/case-insensitive) in the passage."""
    q = _norm_ws(quote.strip().strip('"').strip("'"))
    if len(q) < 8:
        return None
    return quote.strip() if q in _norm_ws(passage) else None


def humanize(rel: str) -> str:
    return rel.lower().replace("_", " ")


class OntologyResolver:
    def __init__(self, workspace_id: str, registry: RelationRegistry, existing_entities: list[ExistingEntity],
                 existing_claims: list[ExistingClaim] | None = None) -> None:
        self.ws = workspace_id
        self.registry = registry
        self.entities: dict[str, EntityWrite] = {}
        self.key_index: dict[str, str] = {}
        self._existing_ids: set[str] = set()
        for e in existing_entities:
            self.entities[e.id] = EntityWrite(id=e.id, name=e.name, category=e.category, keys=list(e.keys),
                                              aliases=list(e.aliases), is_new=False)
            self._existing_ids.add(e.id)
            for k in e.keys:
                self.key_index.setdefault(k, e.id)
        self._touched: set[str] = set()
        self.existing_claims = existing_claims or []

    # ------------------------------------------------------------------ entities

    def _index(self, ent: EntityWrite) -> None:
        for k in ent.keys:
            self.key_index.setdefault(k, ent.id)

    def resolve_entity(self, name: str, category: str = "Concept", aliases: list[str] | None = None,
                       description: str = "", time_context: str | None = None,
                       proposals: list[ProposalWrite] | None = None) -> EntityWrite | None:
        proposals = proposals if proposals is not None else []
        main, paren_aliases = split_alias(name.strip())
        all_aliases = list(dict.fromkeys([*paren_aliases, *(aliases or [])]))
        main_keys = entity_keys(main)
        if not main_keys:
            return None
        alias_keys = [k for a in all_aliases for k in entity_keys(a)]

        # exact key matches (main name first, then aliases)
        matched_ids: list[str] = []
        for k in [*main_keys, *alias_keys]:
            eid = self.key_index.get(k)
            if eid and eid not in matched_ids:
                matched_ids.append(eid)
        compatible_ids = [i for i in matched_ids if compatible(self.entities[i].category, category)]

        if compatible_ids:
            target = self.entities[compatible_ids[0]]
            for other in compatible_ids[1:]:
                a, b = sorted([target.id, other])
                proposals.append(ProposalWrite(
                    kind="entity_merge", dedupe_key=stable_id("merge", a, b),
                    summary=f"'{self.entities[a].name}' and '{self.entities[b].name}' may be the same entity",
                    payload={"entity_ids": [a, b], "reason": "shared alias"}))
            self._merge_into(target, main, all_aliases, main_keys + alias_keys, description, time_context, category)
            return target

        homonym = bool(matched_ids)
        eid = stable_id(self.ws, "entity", main_keys[0], category) if homonym else stable_id(self.ws, "entity",
                                                                                             main_keys[0])
        if eid in self.entities:  # same homonym seen earlier in this batch
            target = self.entities[eid]
            self._merge_into(target, main, all_aliases, main_keys + alias_keys, description, time_context, category)
            return target
        ent = EntityWrite(id=eid, name=main[:200], category=category,
                          keys=list(dict.fromkeys(main_keys + alias_keys)), aliases=all_aliases,
                          description=description, time_context=time_context, is_new=True)
        self.entities[eid] = ent
        self._touched.add(eid)
        if homonym:
            proposals.append(ProposalWrite(
                kind="homonym", dedupe_key=stable_id("homonym", *sorted([eid, matched_ids[0]])),
                summary=f"'{main}' ({category}) kept separate from '{self.entities[matched_ids[0]].name}' "
                        f"({self.entities[matched_ids[0]].category}) because their types differ",
                payload={"entity_ids": [eid, matched_ids[0]]}))
        else:
            self._near_duplicates(ent, proposals)
        self._index(ent)
        return ent

    def _merge_into(self, target: EntityWrite, name: str, aliases: list[str], keys: list[str], description: str,
                    time_context: str | None, category: str) -> None:
        new_aliases = [a for a in [name, *aliases] if a != target.name and a not in target.aliases]
        target.aliases = list(dict.fromkeys([*target.aliases, *new_aliases]))[:30]
        target.keys = list(dict.fromkeys([*target.keys, *keys]))
        if description and not target.description:
            target.description = description
        if time_context and not target.time_context:
            target.time_context = time_context
        if target.category in ("Other", "Concept") and category not in ("Other", "Concept") and compatible(
                target.category, category):
            target.category = category  # refine a generic category
        self._touched.add(target.id)
        self._index(target)

    def _near_duplicates(self, ent: EntityWrite, proposals: list[ProposalWrite]) -> None:
        key = ent.keys[0]
        acr = acronym(key)
        for other_id, other in list(self.entities.items()):
            if other_id == ent.id or not compatible(other.category, ent.category):
                continue
            okey = other.keys[0]
            reason = None
            if len(key) >= 5 and len(okey) >= 5 and difflib.SequenceMatcher(None, key, okey).ratio() >= 0.9:
                reason = "similar names"
            elif acr and len(acr) >= 2 and acr in other.keys:
                reason = "acronym"
            elif (oacr := acronym(okey)) and len(oacr) >= 2 and oacr in ent.keys:
                reason = "acronym"
            if reason:
                a, b = sorted([ent.id, other_id])
                proposals.append(ProposalWrite(
                    kind="entity_merge", dedupe_key=stable_id("merge", a, b),
                    summary=f"'{ent.name}' may duplicate '{other.name}' ({reason})",
                    payload={"entity_ids": [a, b], "reason": reason}))

    # ------------------------------------------------------------------ main entry

    def resolve(self, extraction: ExtractionResult, passages: dict[str, PassageRef], *, extraction_id: str,
                session_id: str | None, extracted_at: str) -> GraphWriteBatch:
        batch = GraphWriteBatch(workspace_id=self.ws)
        proposals: dict[str, ProposalWrite] = {}
        plist: list[ProposalWrite] = []
        categories: dict[str, str] = {}

        for e in extraction.entities:
            ent = self.resolve_entity(e.name, e.category, e.aliases, e.description, e.time_context, plist)
            if ent is None:
                batch.rejected.append({"candidate": e.name, "reason": "empty name after normalization"})
                continue
            categories[normalize_key(e.name)] = ent.category

        claims: dict[str, ClaimWrite] = {}
        evidence: dict[str, EvidenceWrite] = {}

        def entity_for(name: str) -> EntityWrite | None:
            main, _ = split_alias(name)
            for k in entity_keys(main):
                if k in self.key_index:
                    return self.entities[self.key_index[k]]
            return self.resolve_entity(name, "Concept", [], "", None, plist)

        def add_evidence(claim_id: str, passage_id: str, quote: str, confidence: float, raw: str | None,
                         perspective: str | None) -> None:
            ref = passages[passage_id]
            verified = verify_quote(quote, ref.text) if quote else None
            ev_id = stable_id(self.ws, "evidence", claim_id, ref.chunk_id)
            existing = evidence.get(ev_id)
            if existing and (existing.quote_verified or not verified):
                existing.confidence = max(existing.confidence, confidence)
                return
            evidence[ev_id] = EvidenceWrite(
                id=ev_id, claim_id=claim_id, source_id=ref.source_id, chunk_id=ref.chunk_id, quote=verified,
                quote_verified=verified is not None, confidence=confidence, raw_predicate=raw,
                perspective=perspective, extraction_id=extraction_id, extracted_at=extracted_at)

        for r in extraction.relations:
            if r.passage not in passages:
                batch.rejected.append({"candidate": f"{r.subject} {r.predicate} {r.object}",
                                       "reason": f"unknown passage '{r.passage}'"})
                continue
            s, o = entity_for(r.subject), entity_for(r.object)
            if s is None or o is None:
                batch.rejected.append({"candidate": f"{r.subject} {r.predicate} {r.object}",
                                       "reason": "unresolvable entity"})
                continue
            pred = self.registry.resolve(r.predicate)
            if pred.reversed:
                s, o = o, s
            direction_corrected = False
            spec = self.registry.specs.get(pred.canonical)
            if spec and not self._categories_ok(spec, s.category, o.category):
                if self._categories_ok(spec, o.category, s.category):
                    s, o = o, s
                    direction_corrected = True
                else:
                    plist.append(ProposalWrite(
                        kind="type_mismatch",
                        dedupe_key=stable_id("typemismatch", s.id, pred.canonical, o.id),
                        summary=f"'{s.name}' {humanize(pred.canonical)} '{o.name}': entity types "
                                f"({s.category} -> {o.category}) are unusual for this relation",
                        payload={"subject_id": s.id, "object_id": o.id, "predicate": pred.canonical,
                                 "raw": r.predicate}))
            if s.id == o.id:
                batch.rejected.append({"candidate": f"{r.subject} {r.predicate} {r.object}",
                                       "reason": "self-referential relation"})
                continue
            if pred.symmetric and s.id > o.id:
                s, o = o, s
            if pred.status == "proposed":
                plist.append(ProposalWrite(
                    kind="new_relation_type", dedupe_key=stable_id("reltype", pred.canonical),
                    summary=f"New relation type {pred.canonical} (family: {pred.family}) from '{r.predicate}'",
                    payload={"name": pred.canonical, "family": pred.family, "examples": [r.predicate]}))
            cid = stable_id(self.ws, "claim", s.id, pred.canonical, o.id, r.negated)
            text = f"{s.name} {'does NOT ' if r.negated else ''}{humanize(pred.canonical)} {o.name}"
            if cid in claims:
                c = claims[cid]
                c.confidence = max(c.confidence, r.confidence)
            else:
                claims[cid] = ClaimWrite(
                    id=cid, text=text, kind="relation", subject_id=s.id, object_id=o.id,
                    predicate_canonical=pred.canonical, predicate_raw=pred.raw, family=pred.family,
                    negated=r.negated, relation_status=pred.status if pred.status != "canonical" else "canonical",
                    direction_corrected=direction_corrected, confidence=r.confidence, time_context=r.time_context,
                    perspective=r.perspective, session_id=session_id)
            add_evidence(cid, r.passage, r.quote, r.confidence, r.predicate, r.perspective)

        for f in extraction.facts:
            if f.passage not in passages:
                batch.rejected.append({"candidate": f.statement[:80], "reason": f"unknown passage '{f.passage}'"})
                continue
            s = entity_for(f.about)
            if s is None:
                batch.rejected.append({"candidate": f.statement[:80], "reason": "unresolvable entity"})
                continue
            cid = stable_id(self.ws, "fact", s.id, normalize_key(f.statement))
            if cid not in claims:
                claims[cid] = ClaimWrite(
                    id=cid, text=" ".join(f.statement.split()), kind="fact", subject_id=s.id, object_id=None,
                    predicate_canonical="STATES", predicate_raw="states", family="fact", confidence=f.confidence,
                    time_context=f.time_context, perspective=f.perspective, session_id=session_id)
            add_evidence(cid, f.passage, f.quote, f.confidence, None, f.perspective)

        contradictions = self._contradictions(list(claims.values()), plist)

        for p in plist:
            if p.dedupe_key in proposals and p.kind == "new_relation_type":
                ex = proposals[p.dedupe_key].payload.setdefault("examples", [])
                ex.extend(e for e in p.payload.get("examples", []) if e not in ex)
            else:
                proposals.setdefault(p.dedupe_key, p)

        batch.entities = [self.entities[i] for i in self._touched]
        batch.claims = list(claims.values())
        batch.evidence = list(evidence.values())
        batch.contradictions = contradictions
        batch.proposals = list(proposals.values())
        return batch

    @staticmethod
    def _categories_ok(spec, subj_cat: str, obj_cat: str) -> bool:
        ok_s = spec.subject_categories is None or subj_cat in spec.subject_categories or subj_cat == "Other"
        ok_o = spec.object_categories is None or obj_cat in spec.object_categories or obj_cat == "Other"
        return ok_s and ok_o

    def _contradictions(self, new_claims: list[ClaimWrite], plist: list[ProposalWrite]) -> list[ContradictionWrite]:
        out: dict[tuple[str, str], ContradictionWrite] = {}
        rel_claims = [(c.id, c.subject_id, c.predicate_canonical, c.object_id, c.negated)
                      for c in new_claims if c.kind == "relation"]
        everything = rel_claims + [(c.id, c.subject_id, c.predicate, c.object_id, c.negated)
                                   for c in self.existing_claims]
        by_triple: dict[tuple, list[tuple]] = {}
        by_subject_pred: dict[tuple, list[tuple]] = {}
        for c in everything:
            by_triple.setdefault((c[1], c[2], c[3]), []).append(c)
            by_subject_pred.setdefault((c[1], c[2]), []).append(c)
        new_ids = {c[0] for c in rel_claims}

        def record(a: tuple, b: tuple, reason: str) -> None:
            if a[0] == b[0] or (a[0] not in new_ids and b[0] not in new_ids):
                return
            pair = tuple(sorted([a[0], b[0]]))
            if pair in out:
                return
            out[pair] = ContradictionWrite(claim_a=pair[0], claim_b=pair[1], reason=reason)
            plist.append(ProposalWrite(kind="contradiction", dedupe_key=stable_id("contra", *pair),
                                       summary=f"Possible contradiction: {reason}",
                                       payload={"claim_ids": list(pair), "reason": reason}))

        for group in by_triple.values():
            pos = [c for c in group if not c[4]]
            neg = [c for c in group if c[4]]
            for a in pos:
                for b in neg:
                    record(a, b, "one source asserts the relation, another denies it")
        for (subj, pred), group in by_subject_pred.items():
            spec = self.registry.specs.get(pred)
            if not spec or not spec.single_valued:
                continue
            positives = [c for c in group if not c[4]]
            objects = {c[3] for c in positives}
            if len(objects) > 1:
                for i, a in enumerate(positives):
                    for b in positives[i + 1:]:
                        if a[3] != b[3]:
                            record(a, b, f"'{humanize(pred)}' normally has one value but sources disagree")
        return list(out.values())


def resolved_predicate_summary(p: ResolvedPredicate) -> dict:
    return {"canonical": p.canonical, "family": p.family, "reversed": p.reversed, "status": p.status,
            "matched_by": p.matched_by, "raw": p.raw}
