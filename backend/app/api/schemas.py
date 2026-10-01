"""Typed API request/response models (the OpenAPI contract consumed by the frontend)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- workspaces / sessions


class WorkspaceCreate(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    title: str | None = Field(default=None, max_length=300)
    description: str = Field(default="", max_length=4000)
    audience: str = Field(default="", max_length=300)
    academic_level: str = Field(default="", max_length=200)
    teaching_goals: list[str] = Field(default_factory=list, max_length=20)
    familiarity_level: Literal[0, 1, 2] = 0
    time_available_minutes: int | None = Field(default=None, ge=5, le=100_000)
    language: str = Field(default="en", max_length=40)
    content_preferences: dict[str, Any] = Field(default_factory=dict)


class WorkspaceUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    audience: str | None = Field(default=None, max_length=300)
    academic_level: str | None = Field(default=None, max_length=200)
    teaching_goals: list[str] | None = Field(default=None, max_length=20)
    familiarity_level: Literal[0, 1, 2] | None = None
    time_available_minutes: int | None = Field(default=None, ge=5, le=100_000)
    language: str | None = Field(default=None, max_length=40)
    content_preferences: dict[str, Any] | None = None
    archived: bool | None = None


class WorkspaceOut(ORM):
    id: str
    title: str
    topic: str
    description: str
    audience: str
    academic_level: str
    teaching_goals: list[str]
    familiarity_level: int
    time_available_minutes: int | None
    language: str
    content_preferences: dict[str, Any]
    archived: bool
    created_at: datetime
    updated_at: datetime


class WorkflowStep(BaseModel):
    key: str
    label: str
    status: Literal["done", "available", "recommended", "optional"]


class WorkspaceOverview(BaseModel):
    workspace: WorkspaceOut
    stats: dict[str, Any]
    latest_session_id: str | None
    roadmap_version: int | None
    workflow: list[WorkflowStep]
    active_jobs: list["JobOut"]


class SessionCreate(BaseModel):
    title: str = Field(default="Research session", max_length=300)


class SessionOut(ORM):
    id: str
    workspace_id: str
    title: str
    state: dict[str, Any]
    rolling_summary: str
    summarized_upto_seq: int
    message_count: int
    created_at: datetime
    updated_at: datetime


class MessageOut(ORM):
    id: str
    session_id: str
    seq: int
    role: str
    content: str
    citations: list[dict[str, Any]]
    meta: dict[str, Any]
    created_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class ChatTurnOut(BaseModel):
    message: MessageOut
    job: "JobOut"


# ---------------------------------------------------------------- jobs


class JobOut(ORM):
    id: str
    kind: str
    status: str
    attempts: int
    max_attempts: int
    progress: dict[str, Any]
    result: dict[str, Any]
    error: dict[str, Any]
    workspace_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# ---------------------------------------------------------------- research / sources


class ResearchRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=5)
    session_id: str | None = None
    max_new_sources: int = Field(default=6, ge=1, le=15)


class UrlIngestRequest(BaseModel):
    url: HttpUrl
    session_id: str | None = None


class SourceOut(ORM):
    id: str
    origin: str
    url: str | None
    title: str
    site_name: str
    published_at: str | None
    retrieved_at: datetime
    content_type: str
    status: str
    indexed_at: datetime | None
    graph_built_at: datetime | None
    embedding_model: str | None
    meta: dict[str, Any]


class ChunkOut(ORM):
    id: str
    idx: int
    heading: str
    text: str
    token_count: int


class SourceDetail(SourceOut):
    text_preview: str
    chunks: list[ChunkOut]


# ---------------------------------------------------------------- graph / ontology


class ClaimReview(BaseModel):
    status: Literal["unverified", "rejected", "teacher_verified"]
    note: str = Field(default="", max_length=1000)


class ProposalOut(ORM):
    id: str
    kind: str
    summary: str
    payload: dict[str, Any]
    status: str
    resolution: dict[str, Any]
    created_at: datetime
    resolved_at: datetime | None


class ProposalResolve(BaseModel):
    decision: Literal["accept", "reject"]
    map_to: str | None = Field(default=None, max_length=60, description="Canonical relation for a rejected new type")
    keep_entity_id: str | None = None
    reject_claim_id: str | None = None
    note: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------- roadmap / coverage


class RoadmapItemOut(ORM):
    id: str
    concept_key: str
    title: str
    description: str
    objectives: list[str]
    examples: list[str]
    prerequisite_keys: list[str]
    source_ids: list[str]
    order_index: int
    included: bool
    optional: bool
    teacher_edited: bool
    estimated_minutes: int | None
    coverage: dict[str, Any] = Field(default_factory=dict)


class RoadmapPhaseOut(ORM):
    id: str
    key: str
    title: str
    description: str
    objectives: list[str]
    order_index: int
    included: bool
    teacher_edited: bool
    estimated_minutes: int | None
    status: str
    items: list[RoadmapItemOut]


class RoadmapOut(ORM):
    id: str
    version: int
    organization: str
    summary: str
    origin: str
    created_at: datetime
    phases: list[RoadmapPhaseOut]


class RoadmapVersionOut(ORM):
    id: str
    version: int
    is_current: bool
    origin: str
    organization: str
    created_at: datetime


class RoadmapItemEdit(BaseModel):
    concept_key: str | None = None
    title: str = Field(max_length=300)
    description: str = Field(default="", max_length=4000)
    objectives: list[str] = Field(default_factory=list, max_length=10)
    examples: list[str] = Field(default_factory=list, max_length=10)
    included: bool = True
    optional: bool = False
    estimated_minutes: int | None = Field(default=None, ge=1, le=3000)


class RoadmapPhaseEdit(BaseModel):
    id: str | None = None
    title: str = Field(max_length=300)
    description: str = Field(default="", max_length=4000)
    objectives: list[str] = Field(default_factory=list, max_length=10)
    included: bool = True
    estimated_minutes: int | None = Field(default=None, ge=1, le=10_000)
    items: list[RoadmapItemEdit] = Field(default_factory=list, max_length=40)


class RoadmapEdit(BaseModel):
    expected_version: int | None = None
    phases: list[RoadmapPhaseEdit] = Field(min_length=1, max_length=20)


class RoadmapGenerate(BaseModel):
    instructions: str = Field(default="", max_length=2000)


class NavigateRequest(BaseModel):
    session_id: str
    action: Literal["start_phase", "next_phase", "previous_phase", "goto_phase", "skip_phase", "complete_phase",
                    "revisit_phase"]
    phase_ref: str | None = Field(default=None, max_length=200)


class PhaseStatusUpdate(BaseModel):
    status: Literal["not_started", "in_progress", "completed", "skipped"]


class StartPhaseRequest(BaseModel):
    session_id: str


class CoverageMark(BaseModel):
    concept_key: str = Field(max_length=36)
    concept_name: str = Field(max_length=300)
    stage: Literal["completed", "explained", "discussed"]
    value: bool = True


class CoverageOut(ORM):
    concept_key: str
    concept_name: str
    planned_at: datetime | None
    researched_at: datetime | None
    explained_at: datetime | None
    discussed_at: datetime | None
    completed_at: datetime | None
    completed_origin: str | None
    self_reported_understanding: str | None
    teacher_feedback: str


# ---------------------------------------------------------------- notes / assessments / exports


class NotesOut(ORM):
    id: str
    kind: str
    title: str
    version: int
    is_current: bool
    sections: list[dict[str, Any]]
    config: dict[str, Any]
    concept_keys: list[str]
    source_ids: list[str]
    edited: bool
    created_at: datetime


class NotesSectionEdit(BaseModel):
    id: str | None = None
    heading: str = Field(default="", max_length=300)
    markdown: str = Field(default="", max_length=60000)


class NotesEdit(BaseModel):
    title: str = Field(default="", max_length=300)
    sections: list[NotesSectionEdit] = Field(min_length=1, max_length=60)


class QuestionOut(ORM):
    id: str
    slot_id: str
    section_id: str
    section_order: int
    order_index: int
    type: str
    text: str
    choices: list[str]
    answer: dict[str, Any]
    explanation: str
    rubric: list[dict[str, Any]]
    difficulty: str
    marks: float
    concept_keys: list[str]
    source_refs: list[dict[str, Any]]
    flags: list[dict[str, Any]]
    verification: dict[str, Any]
    origin: str


class AssessmentSummary(ORM):
    id: str
    title: str
    status: str
    total_marks: float
    created_at: datetime
    updated_at: datetime


class AssessmentOut(AssessmentSummary):
    config: dict[str, Any]
    blueprint: dict[str, Any]
    validation_report: dict[str, Any]
    questions: list[QuestionOut]


class AssessmentCreated(BaseModel):
    assessment: AssessmentSummary
    job: JobOut


class QuestionPatch(BaseModel):
    type: str | None = None
    text: str | None = Field(default=None, max_length=6000)
    choices: list[str] | None = Field(default=None, max_length=8)
    answer: dict[str, Any] | None = None
    explanation: str | None = Field(default=None, max_length=6000)
    rubric: list[dict[str, Any]] | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    marks: float | None = Field(default=None, gt=0, le=100)


class QuestionCreate(QuestionPatch):
    section_id: str | None = None
    type: str = "short_answer"
    text: str = Field(min_length=5, max_length=6000)


class RegenerateRequest(BaseModel):
    instructions: str = Field(default="", max_length=1000)


class ArtifactOut(ORM):
    id: str
    kind: str
    title: str
    content_type: str
    size_bytes: int
    checksum: str
    refs: dict[str, Any]
    created_at: datetime


WorkspaceOverview.model_rebuild()
ChatTurnOut.model_rebuild()
