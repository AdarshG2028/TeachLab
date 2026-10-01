"""Schemas for LLM extraction output and resolved graph writes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

EntityCategory = Literal["Concept", "Person", "Event", "Place", "Organization", "Technology", "Method", "Other"]
ENTITY_CATEGORIES: tuple[str, ...] = ("Concept", "Person", "Event", "Place", "Organization", "Technology", "Method",
                                      "Other")


def _clean(v: str) -> str:
    return " ".join(v.split()).strip()


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: EntityCategory = "Concept"
    aliases: list[str] = Field(default_factory=list, max_length=10)
    description: str = Field(default="", max_length=600)
    time_context: str | None = Field(default=None, max_length=120)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean(v)

    @field_validator("category", mode="before")
    @classmethod
    def _cat(cls, v: object) -> str:
        if isinstance(v, str):
            for c in ENTITY_CATEGORIES:
                if v.strip().lower() == c.lower():
                    return c
        return "Other"

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v: list[str]) -> list[str]:
        return [_clean(a)[:200] for a in v if a and _clean(a)]


class ExtractedRelation(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=120)
    object: str = Field(min_length=1, max_length=200)
    passage: str = Field(description="Passage id, e.g. 'P2', that supports this relation")
    quote: str = Field(default="", max_length=600, description="Short verbatim quote from the passage")
    negated: bool = False
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    time_context: str | None = Field(default=None, max_length=120)
    perspective: str | None = Field(default=None, max_length=300,
                                    description="Attribution if the claim is a viewpoint, e.g. 'according to X'")


class ExtractedFact(BaseModel):
    about: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=3, max_length=600)
    passage: str
    quote: str = Field(default="", max_length=600)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    time_context: str | None = Field(default=None, max_length=120)
    perspective: str | None = Field(default=None, max_length=300)


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=80)
    relations: list[ExtractedRelation] = Field(default_factory=list, max_length=120)
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=80)


# --------------------------------------------------------------------------- resolved writes


class EntityWrite(BaseModel):
    id: str
    name: str
    category: str
    keys: list[str]
    aliases: list[str]
    description: str = ""
    time_context: str | None = None
    is_new: bool = False


class EvidenceWrite(BaseModel):
    id: str
    claim_id: str
    source_id: str
    chunk_id: str
    quote: str | None
    quote_verified: bool
    confidence: float
    raw_predicate: str | None = None
    perspective: str | None = None
    extraction_id: str
    extracted_at: str


class ClaimWrite(BaseModel):
    id: str
    text: str
    kind: Literal["relation", "fact"]
    subject_id: str
    object_id: str | None = None
    predicate_canonical: str
    predicate_raw: str
    family: str
    negated: bool = False
    relation_status: Literal["canonical", "accepted", "proposed"] = "canonical"
    direction_corrected: bool = False
    confidence: float
    time_context: str | None = None
    perspective: str | None = None
    session_id: str | None = None


class ContradictionWrite(BaseModel):
    claim_a: str
    claim_b: str
    reason: str


class ProposalWrite(BaseModel):
    kind: Literal["new_relation_type", "entity_merge", "contradiction", "homonym", "type_mismatch"]
    dedupe_key: str
    summary: str
    payload: dict


class GraphWriteBatch(BaseModel):
    workspace_id: str
    entities: list[EntityWrite] = Field(default_factory=list)
    claims: list[ClaimWrite] = Field(default_factory=list)
    evidence: list[EvidenceWrite] = Field(default_factory=list)
    contradictions: list[ContradictionWrite] = Field(default_factory=list)
    proposals: list[ProposalWrite] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)  # candidates dropped by validation, with reasons
