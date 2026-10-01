"""Assessment configuration, blueprint and question schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

QType = Literal["mcq", "true_false", "fill_blank", "short_answer", "long_answer", "numerical", "coding",
                "application"]
QTYPES: tuple[str, ...] = ("mcq", "true_false", "fill_blank", "short_answer", "long_answer", "numerical", "coding",
                           "application")
Difficulty = Literal["easy", "medium", "hard"]

DEFAULT_MARKS: dict[str, float] = {"mcq": 1, "true_false": 1, "fill_blank": 1, "short_answer": 3,
                                   "long_answer": 8, "numerical": 4, "coding": 8, "application": 5}
TYPE_LABELS: dict[str, str] = {"mcq": "Multiple choice", "true_false": "True or false", "fill_blank": "Fill in the blanks",
                               "short_answer": "Short answer", "long_answer": "Long answer", "numerical": "Numerical",
                               "coding": "Coding", "application": "Application"}
RUBRIC_TYPES = {"short_answer", "long_answer", "coding", "application"}


class SectionConfig(BaseModel):
    id: str | None = Field(default=None, max_length=40)
    title: str = Field(default="", max_length=200)
    question_type: QType
    count: int = Field(ge=1, le=100)
    marks_each: float | None = Field(default=None, gt=0, le=100)
    difficulty: Literal["easy", "medium", "hard", "mixed"] | None = None
    instructions: str = Field(default="", max_length=500)


class AssessmentConfig(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    topics: list[str] = Field(default_factory=list, max_length=60)  # concept keys; empty = all covered concepts
    student_level: str = Field(default="", max_length=200)
    difficulty: Literal["easy", "medium", "hard", "mixed"] = "mixed"
    difficulty_distribution: dict[Difficulty, float] | None = None  # weights, e.g. {"easy":0.3,...}
    question_types: list[QType] = Field(default_factory=lambda: ["mcq", "short_answer"])
    num_questions: int | None = Field(default=None, ge=1, le=200)
    marks_per_type: dict[QType, float] = Field(default_factory=dict)
    total_marks: float | None = Field(default=None, gt=0, le=1000)
    duration_minutes: int | None = Field(default=None, ge=5, le=600)
    sections: list[SectionConfig] = Field(default_factory=list, max_length=12)
    topic_distribution: dict[str, float] = Field(default_factory=dict)  # concept key -> weight
    coverage_mode: Literal["strict", "expanded"] = "strict"
    notes_ids: list[str] = Field(default_factory=list)  # strict scope may come from selected notes
    answers_in_separate_document: bool = True
    include_answers_in_paper: bool = False
    custom_instructions: str = Field(default="", max_length=2000)
    institution: str = Field(default="", max_length=200)
    class_name: str = Field(default="", max_length=120)
    teacher_name: str = Field(default="", max_length=120)

    @field_validator("question_types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        out = list(dict.fromkeys(v))
        if not out:
            raise ValueError("At least one question type is required")
        return out

    @model_validator(mode="after")
    def _consistency(self) -> "AssessmentConfig":
        if not self.sections and not self.num_questions:
            raise ValueError("Provide either sections or num_questions")
        if self.sections and self.num_questions and sum(s.count for s in self.sections) != self.num_questions:
            raise ValueError(f"Section question counts sum to {sum(s.count for s in self.sections)} but "
                             f"num_questions is {self.num_questions}")
        if self.difficulty_distribution:
            if any(w < 0 for w in self.difficulty_distribution.values()) or sum(
                    self.difficulty_distribution.values()) <= 0:
                raise ValueError("difficulty_distribution weights must be non-negative and not all zero")
        return self


class BlueprintSlot(BaseModel):
    slot_id: str
    section_id: str
    section_order: int
    order_index: int
    type: QType
    marks: float
    difficulty: Difficulty
    concept_key: str
    concept_name: str


class BlueprintSection(BaseModel):
    id: str
    title: str
    question_type: QType
    instructions: str
    count: int
    marks_each: float
    total: float


class Blueprint(BaseModel):
    sections: list[BlueprintSection]
    slots: list[BlueprintSlot]
    total_marks: float
    scope_concepts: list[dict]  # [{"key","name"}]
    coverage_mode: str


class RubricItem(BaseModel):
    criterion: str = Field(max_length=500)
    marks: float = Field(ge=0)


class GenQuestion(BaseModel):
    slot_id: str
    type: QType
    text: str = Field(min_length=5, max_length=6000)
    choices: list[str] = Field(default_factory=list, max_length=8)
    correct_choice: int | None = None
    answer: str = Field(default="", max_length=8000)
    numeric_answer: float | None = None
    tolerance: float | None = Field(default=None, ge=0)
    units: str | None = Field(default=None, max_length=40)
    answer_expression: str | None = Field(default=None, max_length=500)
    code_language: str | None = Field(default=None, max_length=30)
    reference_solution: str | None = Field(default=None, max_length=8000)
    code_tests: str | None = Field(default=None, max_length=8000)
    explanation: str = Field(default="", max_length=6000)
    rubric: list[RubricItem] = Field(default_factory=list, max_length=12)
    difficulty: Difficulty = "medium"
    concepts: list[str] = Field(default_factory=list, max_length=6)
    cited_sources: list[str] = Field(default_factory=list, max_length=8)


class GenQuestionBatch(BaseModel):
    questions: list[GenQuestion] = Field(min_length=1, max_length=30)


class StudentQuestionView(BaseModel):
    """What may appear on a student paper. Deliberately has NO answer, explanation or rubric fields."""

    number: str
    section_id: str
    type: QType
    text: str
    choices: list[str] = Field(default_factory=list)
    marks: float
