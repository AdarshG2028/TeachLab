"""PostgreSQL schema (source of truth).

Neo4j holds the knowledge graph and Qdrant holds vectors; both can be rebuilt
from the rows stored here (sources, chunks, extractions).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.ids import new_id


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


def _ws_fk() -> Mapped[str]:
    return mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False)


# --------------------------------------------------------------------------- users / workspaces


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # external auth subject
    display_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _created()


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = _pk()
    owner_id: Mapped[str] = mapped_column(String(128), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    topic: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String(300), default="")
    academic_level: Mapped[str] = mapped_column(String(200), default="")
    teaching_goals: Mapped[list[Any]] = mapped_column(default=list)
    familiarity_level: Mapped[int] = mapped_column(Integer, default=0)
    time_available_minutes: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(40), default="en")
    content_preferences: Mapped[dict[str, Any]] = mapped_column(default=dict)
    subject_profile: Mapped[dict[str, Any]] = mapped_column(default=dict)  # inferred domain shape
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Preference(Base):
    """Teacher preferences. ``origin`` distinguishes explicit statements from inferred ones."""

    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("user_id", "scope", "key", "origin"),)
    id: Mapped[str] = _pk()
    user_id: Mapped[str] = mapped_column(String(128), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(64), default="global")  # "global" or a workspace id
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    origin: Mapped[str] = mapped_column(String(16), default="explicit")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


# --------------------------------------------------------------------------- conversation


class ResearchSession(Base):
    __tablename__ = "research_sessions"
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    title: Mapped[str] = mapped_column(String(300), default="Research session")
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rolling_summary: Mapped[str] = mapped_column(Text, default="")
    summarized_upto_seq: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)
    id: Mapped[str] = _pk()
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("research_sessions.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = _ws_fk()
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[Any]] = mapped_column(default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- research material


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("workspace_id", "url_hash"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    origin: Mapped[str] = mapped_column(String(16))  # search | url | upload
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text, default="")
    site_name: Mapped[str] = mapped_column(String(300), default="")
    published_at: Mapped[str | None] = mapped_column(String(40))  # as reported by the source; not always a full date
    retrieved_at: Mapped[datetime] = _created()
    content_type: Mapped[str] = mapped_column(String(40), default="text/html")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    storage_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="ingested")  # ingested|indexed|graph_built|failed
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    graph_built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    chunks: Mapped[list[Chunk]] = relationship(back_populates="source", cascade="all, delete-orphan",
                                                 order_by="Chunk.idx")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("source_id", "idx"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # stable_id(source_id, idx, hash)
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = _ws_fk()
    idx: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()

    source: Mapped[Source] = relationship(back_populates="chunks")


class SearchCache(Base):
    __tablename__ = "search_cache"
    __table_args__ = (UniqueConstraint("workspace_id", "query_hash"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    query: Mapped[str] = mapped_column(Text)
    query_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(40))
    results: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()


class Extraction(Base):
    """Validated LLM extraction output for a batch of chunks. Replayable into the graph."""

    __tablename__ = "extractions"
    __table_args__ = (UniqueConstraint("workspace_id", "batch_key"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36))
    batch_key: Mapped[str] = mapped_column(String(64))
    chunk_ids: Mapped[list[Any]] = mapped_column(default=list)
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apply_report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class RelationType(Base):
    """Workspace-specific relation types accepted beyond the built-in canonical set."""

    __tablename__ = "relation_types"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    name: Mapped[str] = mapped_column(String(80))
    family: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed|accepted|rejected
    synonyms: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()


class OntologyProposal(Base):
    """Uncertain ontology changes awaiting teacher review."""

    __tablename__ = "ontology_proposals"
    __table_args__ = (UniqueConstraint("workspace_id", "dedupe_key"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    kind: Mapped[str] = mapped_column(String(32))  # new_relation_type | entity_merge | contradiction | low_confidence
    dedupe_key: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    resolution: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- roadmap / coverage


class RoadmapVersion(Base):
    __tablename__ = "roadmap_versions"
    __table_args__ = (UniqueConstraint("workspace_id", "version"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    version: Mapped[int] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    organization: Mapped[str] = mapped_column(String(40), default="prerequisite")
    summary: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(16), default="generated")  # generated | teacher_edit | regenerated
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()

    phases: Mapped[list[RoadmapPhase]] = relationship(back_populates="version", cascade="all, delete-orphan",
                                                        order_by="RoadmapPhase.order_index")


class RoadmapPhase(Base):
    __tablename__ = "roadmap_phases"
    id: Mapped[str] = _pk()
    version_id: Mapped[str] = mapped_column(String(36), ForeignKey("roadmap_versions.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = _ws_fk()
    key: Mapped[str] = mapped_column(String(64))  # stable across versions
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    objectives: Mapped[list[Any]] = mapped_column(default=list)
    order_index: Mapped[int] = mapped_column(Integer)
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    teacher_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="not_started")  # not_started|in_progress|completed|skipped

    version: Mapped[RoadmapVersion] = relationship(back_populates="phases")
    items: Mapped[list[RoadmapItem]] = relationship(back_populates="phase", cascade="all, delete-orphan",
                                                      order_by="RoadmapItem.order_index")


class RoadmapItem(Base):
    __tablename__ = "roadmap_items"
    id: Mapped[str] = _pk()
    phase_id: Mapped[str] = mapped_column(String(36), ForeignKey("roadmap_phases.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = _ws_fk()
    concept_key: Mapped[str] = mapped_column(String(36))  # graph entity id (stable_id of workspace + name)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    objectives: Mapped[list[Any]] = mapped_column(default=list)
    examples: Mapped[list[Any]] = mapped_column(default=list)
    prerequisite_keys: Mapped[list[Any]] = mapped_column(default=list)
    source_ids: Mapped[list[Any]] = mapped_column(default=list)
    order_index: Mapped[int] = mapped_column(Integer)
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    optional: Mapped[bool] = mapped_column(Boolean, default=False)
    teacher_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)

    phase: Mapped[RoadmapPhase] = relationship(back_populates="items")


class CoverageRecord(Base):
    """Per-concept coverage. Each stage is tracked independently; completion origin is explicit."""

    __tablename__ = "coverage_records"
    __table_args__ = (UniqueConstraint("workspace_id", "concept_key"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    concept_key: Mapped[str] = mapped_column(String(36))
    concept_name: Mapped[str] = mapped_column(String(300))
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    explained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discussed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_origin: Mapped[str | None] = mapped_column(String(16))  # explicit only; never inferred
    self_reported_understanding: Mapped[str | None] = mapped_column(String(32))
    teacher_feedback: Mapped[str] = mapped_column(Text, default="")
    evidence_message_ids: Mapped[list[Any]] = mapped_column(default=list)
    updated_at: Mapped[datetime] = _updated()


# --------------------------------------------------------------------------- teaching material


class NotesDocument(Base):
    __tablename__ = "notes_documents"
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    kind: Mapped[str] = mapped_column(String(16))  # teacher | student
    title: Mapped[str] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="ready")
    sections: Mapped[list[Any]] = mapped_column(default=list)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    concept_keys: Mapped[list[Any]] = mapped_column(default=list)
    source_ids: Mapped[list[Any]] = mapped_column(default=list)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Assessment(Base):
    __tablename__ = "assessments"
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    title: Mapped[str] = mapped_column(String(300))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    blueprint: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|generating|ready|failed
    validation_report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    total_marks: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    questions: Mapped[list[Question]] = relationship(back_populates="assessment", cascade="all, delete-orphan",
                                                       order_by="(Question.section_order, Question.order_index)")


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[str] = _pk()
    assessment_id: Mapped[str] = mapped_column(String(36), ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = _ws_fk()
    slot_id: Mapped[str] = mapped_column(String(64))  # blueprint slot this question fills
    section_id: Mapped[str] = mapped_column(String(64))
    section_order: Mapped[int] = mapped_column(Integer, default=0)
    order_index: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(Text)
    choices: Mapped[list[Any]] = mapped_column(default=list)
    answer: Mapped[dict[str, Any]] = mapped_column(default=dict)
    explanation: Mapped[str] = mapped_column(Text, default="")
    rubric: Mapped[list[Any]] = mapped_column(default=list)
    difficulty: Mapped[str] = mapped_column(String(16))
    marks: Mapped[float] = mapped_column(Float)
    concept_keys: Mapped[list[Any]] = mapped_column(default=list)
    source_refs: Mapped[list[Any]] = mapped_column(default=list)
    flags: Mapped[list[Any]] = mapped_column(default=list)
    verification: Mapped[dict[str, Any]] = mapped_column(default=dict)
    origin: Mapped[str] = mapped_column(String(16), default="generated")  # generated|edited|regenerated|manual
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    assessment: Mapped[Assessment] = relationship(back_populates="questions")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)
    id: Mapped[str] = _pk()
    workspace_id: Mapped[str] = _ws_fk()
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    storage_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(80), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    refs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- jobs


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)
    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"),
                                                     index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed|cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[dict[str, Any]] = mapped_column(default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _updated()
