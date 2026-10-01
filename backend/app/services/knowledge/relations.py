"""Canonical relation registry and predicate normalization.

A small, domain-agnostic core. Each canonical type belongs to a broad family.
Synonym phrases may map to a canonical type *in reverse* (e.g. "consists of"
-> PART_OF with subject/object swapped), so stored edges always use one
direction per type. Unknown predicates become *proposed* types for review.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

FAMILIES = (
    "taxonomic", "compositional", "prerequisite", "causal", "temporal", "spatial", "functional",
    "comparative", "attribution", "teaching", "association",
)

REL_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,47}$")

_AUX = {"is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "can", "could", "may", "might",
        "will", "would", "should", "often", "usually", "typically", "also", "directly", "primarily", "mainly",
        "generally", "commonly", "itself"}


@dataclass(frozen=True)
class RelationSpec:
    name: str
    family: str
    synonyms: tuple[str, ...] = ()
    reverse_synonyms: tuple[str, ...] = ()  # phrases meaning the inverse direction
    symmetric: bool = False
    subject_categories: frozenset[str] | None = None  # None = any
    object_categories: frozenset[str] | None = None
    single_valued: bool = False  # a subject normally has one object (used for contradiction hints)


def _cats(*c: str) -> frozenset[str]:
    return frozenset(c)


_AGENTS = _cats("Person", "Organization")
_PLACES = _cats("Place")
_EVENTS = _cats("Event")

CANONICAL: tuple[RelationSpec, ...] = (
    # taxonomic
    RelationSpec("IS_A", "taxonomic",
                 ("is a", "is an", "is a type of", "is a kind of", "is a form of", "type of", "kind of", "subclass of",
                  "is a subtype of", "is a category of", "is classified as", "belongs to the class of", "is a class of"),
                 ("has type", "includes type", "has subtype", "has subtypes", "has kind")),
    RelationSpec("INSTANCE_OF", "taxonomic", ("is an instance of", "instance of", "is an example of a")),
    # compositional
    RelationSpec("PART_OF", "compositional",
                 ("is part of", "part of", "is a component of", "component of", "belongs to", "is contained in",
                  "is a member of the", "is an element of", "makes up part of", "forms part of"),
                 ("has part", "has component", "consists of", "is composed of", "contains", "comprises", "includes",
                  "is made up of", "has components", "has parts", "is made of")),
    # prerequisite (educational)
    RelationSpec("PREREQUISITE_OF", "prerequisite",
                 ("is a prerequisite of", "is a prerequisite for", "prerequisite for", "is required to understand",
                  "is foundational to", "is needed to understand", "underpins", "is the basis for", "is the foundation of"),
                 ("requires knowledge of", "requires understanding of", "builds on", "builds upon", "presupposes",
                  "has prerequisite", "is based on understanding")),
    # causal
    RelationSpec("CAUSES", "causal",
                 ("causes", "cause", "caused", "leads to", "led to", "results in", "triggers", "gives rise to",
                  "brings about", "is a cause of", "contributes to", "produces effect", "sparked", "provoked",
                  "precipitated"),
                 ("is caused by", "results from", "is a result of", "is triggered by", "stems from", "arises from",
                  "is a consequence of", "is an effect of")),
    RelationSpec("INFLUENCED", "causal",
                 ("influenced", "influences", "inspired", "shaped", "had an influence on", "affected", "affects",
                  "impacted", "impacts", "had an impact on", "informed", "motivated"),
                 ("was inspired by", "is inspired by", "was shaped by", "was motivated by", "drew on", "draws on")),
    RelationSpec("ENABLES", "causal",
                 ("enables", "allows", "makes possible", "facilitates", "permits", "supports", "empowers"),
                 ("is enabled by", "is made possible by", "is facilitated by")),
    RelationSpec("PREVENTS", "causal",
                 ("prevents", "inhibits", "blocks", "hinders", "limits", "reduces", "suppresses", "mitigates"),
                 ("is prevented by", "is inhibited by", "is limited by", "is mitigated by")),
    # temporal
    RelationSpec("PRECEDED", "temporal",
                 ("preceded", "precedes", "came before", "happened before", "occurred before", "predates"),
                 ("followed", "follows", "came after", "happened after", "occurred after", "succeeded",
                  "postdates")),
    RelationSpec("OCCURRED_DURING", "temporal", ("occurred during", "happened during", "took place during", "during"),
                 ("included event", "saw")),
    # spatial
    RelationSpec("LOCATED_IN", "spatial",
                 ("is located in", "located in", "is in", "is situated in", "lies in", "is based in", "took place in",
                  "occurred in", "happened in"),
                 ("is the location of", "hosts"), object_categories=_PLACES, single_valued=True),
    # functional
    RelationSpec("USES", "functional",
                 ("uses", "utilizes", "employs", "leverages", "relies on", "makes use of", "applies", "runs on",
                  "operates using", "is built on", "is built with", "uses the principle of"),
                 ("is used by", "is used in", "is utilized by", "is employed by", "is applied in", "is leveraged by",
                  "is applied to", "powers")),
    RelationSpec("DEPENDS_ON", "functional", ("depends on", "is dependent on", "requires", "needs"),
                 ("is required by", "is needed by", "is a dependency of")),
    RelationSpec("MEASURES", "functional",
                 ("measures", "senses", "detects", "monitors", "quantifies", "records", "tracks", "reads"),
                 ("is measured by", "is detected by", "is sensed by", "is monitored by", "is quantified by")),
    RelationSpec("PRODUCES", "functional",
                 ("produces", "generates", "outputs", "creates output", "emits", "yields", "synthesizes"),
                 ("is produced by", "is generated by", "is emitted by", "is synthesized by")),
    RelationSpec("IMPLEMENTS", "functional", ("implements", "realizes", "is an implementation of", "instantiates"),
                 ("is implemented by", "is realized by", "is implemented in")),
    RelationSpec("REPRESENTED_USING", "functional",
                 ("is represented using", "represented using", "is represented by", "is visualized using",
                  "is visualized with", "is visualised using", "is modeled by", "is modelled by", "is depicted by",
                  "is described by", "is expressed as", "is represented as", "is represented on"),
                 ("represents", "visualizes", "models", "depicts", "describes the state of")),
    RelationSpec("PROVIDES", "functional", ("provides", "offers", "delivers", "supplies", "exposes"),
                 ("is provided by", "is offered by", "is delivered by", "is supplied by")),
    RelationSpec("TRANSFORMS_INTO", "functional",
                 ("transforms into", "converts to", "becomes", "turns into", "evolves into", "converts into"),
                 ("is derived from", "evolved from", "originates from", "is converted from")),
    # comparative
    RelationSpec("SIMILAR_TO", "comparative", ("is similar to", "similar to", "resembles", "is analogous to", "is like",
                                               "is comparable to"), symmetric=True),
    RelationSpec("CONTRASTS_WITH", "comparative",
                 ("contrasts with", "differs from", "is different from", "is unlike", "versus", "vs", "is opposed to",
                  "opposes", "is the opposite of"), symmetric=True),
    RelationSpec("ALTERNATIVE_TO", "comparative", ("is an alternative to", "alternative to", "competes with",
                                                   "can replace", "is a substitute for"), symmetric=True),
    # attribution
    RelationSpec("DEVELOPED_BY", "attribution",
                 ("is developed by", "developed by", "was invented by", "invented by", "was created by", "created by",
                  "was proposed by", "proposed by", "was discovered by", "discovered by", "was formulated by",
                  "was introduced by", "introduced by", "was designed by", "was coined by", "was written by",
                  "was founded by", "founded by", "was pioneered by", "was built by"),
                 ("developed", "invented", "created", "proposed", "discovered", "formulated", "introduced", "designed",
                  "coined", "wrote", "authored", "founded", "pioneered", "built", "conceived"),
                 object_categories=_AGENTS),
    RelationSpec("LED_BY", "attribution", ("was led by", "led by", "is led by", "was headed by", "was ruled by",
                                           "is governed by", "was commanded by"),
                 ("led", "leads", "headed", "ruled", "governed", "commanded"), object_categories=_AGENTS),
    RelationSpec("MEMBER_OF", "attribution", ("is a member of", "member of", "worked for", "works for",
                                              "is affiliated with", "joined", "served in"),
                 ("has member", "employs member"), subject_categories=_AGENTS),
    RelationSpec("PARTICIPATED_IN", "attribution", ("participated in", "took part in", "fought in", "was involved in",
                                                    "was a participant in", "contributed to the"),
                 ("involved", "had participant"), object_categories=_EVENTS),
    # teaching
    RelationSpec("EXAMPLE_OF", "teaching",
                 ("is an example of", "example of", "exemplifies", "illustrates", "is an illustration of",
                  "is a case of", "is a use case of", "is an application of", "application of"),
                 ("has example", "is illustrated by", "is exemplified by", "has application")),
    # fallback
    RelationSpec("RELATED_TO", "association", ("is related to", "related to", "is associated with",
                                               "associated with", "is connected to", "is linked to", "relates to",
                                               "is relevant to"), symmetric=True),
)

_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    ("caus", "causal"), ("lead", "causal"), ("result", "causal"), ("influenc", "causal"), ("trigger", "causal"),
    ("before", "temporal"), ("after", "temporal"), ("during", "temporal"), ("follow", "temporal"),
    ("part", "compositional"), ("contain", "compositional"), ("compos", "compositional"),
    ("locat", "spatial"), ("near", "spatial"), ("border", "spatial"),
    ("prerequisit", "prerequisite"), ("foundation", "prerequisite"),
    ("type", "taxonomic"), ("kind", "taxonomic"), ("class", "taxonomic"),
    ("similar", "comparative"), ("differ", "comparative"), ("compar", "comparative"),
    ("invent", "attribution"), ("found", "attribution"), ("author", "attribution"), ("by", "attribution"),
    ("example", "teaching"), ("illustrat", "teaching"),
)


def _stem(word: str) -> str:
    if len(word) <= 3:
        return word
    for suf, rep in (("ies", "y"), ("ied", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            word = word[: -len(suf)] + rep
            break
    # "measure"/"measures"/"measured" -> "measur"; applied identically to registry phrases and input
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def predicate_key(phrase: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", phrase.lower().replace("_", " ")).split()
    kept = [w for w in words if w not in _AUX]
    if not kept:  # e.g. "is" alone
        kept = words
    return " ".join(_stem(w) for w in kept)


@dataclass(frozen=True)
class ResolvedPredicate:
    canonical: str
    family: str
    reversed: bool
    status: str  # canonical | accepted | proposed
    raw: str
    symmetric: bool = False
    matched_by: str = "exact"  # exact | passive | fuzzy | canonical_name | new


@dataclass
class RelationRegistry:
    """Canonical relations + workspace-accepted extensions, with synonym lookup."""

    specs: dict[str, RelationSpec] = field(default_factory=dict)
    lookup: dict[str, tuple[str, bool]] = field(default_factory=dict)  # key -> (canonical, reversed)
    statuses: dict[str, str] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "RelationRegistry":
        reg = cls()
        for spec in CANONICAL:
            reg.add(spec, status="canonical")
        return reg

    def add(self, spec: RelationSpec, *, status: str) -> None:
        self.specs[spec.name] = spec
        self.statuses[spec.name] = status
        self.lookup.setdefault(predicate_key(spec.name), (spec.name, False))
        for s in spec.synonyms:
            self.lookup.setdefault(predicate_key(s), (spec.name, False))
        for s in spec.reverse_synonyms:
            self.lookup.setdefault(predicate_key(s), (spec.name, not spec.symmetric))

    def add_workspace_type(self, name: str, family: str, synonyms: list[str], status: str) -> None:
        if not REL_TYPE_RE.match(name) or family not in FAMILIES:
            return
        if name in self.specs and self.statuses.get(name) == "canonical":
            for s in synonyms:
                self.lookup.setdefault(predicate_key(s), (name, False))
            return
        self.add(RelationSpec(name, family, tuple(synonyms)), status=status)

    def resolve(self, raw: str) -> ResolvedPredicate:
        raw_clean = " ".join(raw.split())[:120]
        upper = re.sub(r"[^A-Za-z0-9]+", "_", raw_clean).strip("_").upper()
        if upper in self.specs:
            spec = self.specs[upper]
            return ResolvedPredicate(upper, spec.family, False, self.statuses[upper], raw_clean, spec.symmetric,
                                     "canonical_name")
        key = predicate_key(raw_clean)
        hit = self.lookup.get(key)
        if hit:
            return self._mk(hit[0], hit[1], raw_clean, "exact")
        # passive voice: "<verb> by" where "<verb>" is a known active synonym
        if key.endswith(" by"):
            base = self.lookup.get(key[:-3].strip())
            if base:
                return self._mk(base[0], not base[1], raw_clean, "passive")
        # fuzzy match on keys
        close = difflib.get_close_matches(key, list(self.lookup), n=1, cutoff=0.88)
        if close:
            name, rev = self.lookup[close[0]]
            return self._mk(name, rev, raw_clean, "fuzzy")
        # new relation type proposal
        name = proposed_name(raw_clean)
        family = guess_family(key)
        return ResolvedPredicate(name, family, False, "proposed", raw_clean, False, "new")

    def _mk(self, name: str, reversed_: bool, raw: str, how: str) -> ResolvedPredicate:
        spec = self.specs[name]
        return ResolvedPredicate(name, spec.family, reversed_ and not spec.symmetric, self.statuses[name], raw,
                                 spec.symmetric, how)


def proposed_name(raw: str) -> str:
    words = [w for w in re.sub(r"[^A-Za-z0-9 ]+", " ", raw).split() if w.lower() not in _AUX] or ["RELATED"]
    name = "_".join(w.upper() for w in words)[:47]
    name = re.sub(r"_+", "_", name).strip("_")
    if not name or not name[0].isalpha():
        name = "REL_" + name
    return name if REL_TYPE_RE.match(name) else "RELATED_TO"


def guess_family(key: str) -> str:
    for needle, family in _FAMILY_HINTS:
        if needle in key:
            return family
    return "association"
