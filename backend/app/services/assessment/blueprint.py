"""Deterministic test blueprint: sections -> slots with type, marks, difficulty and target concept.

Generation fills slots; validation checks the result against this blueprint,
so counts and marks are guaranteed by construction rather than trusted from
the model.
"""

from __future__ import annotations

import math

from app.core.errors import ValidationFailed
from app.services.assessment.schemas import (
    DEFAULT_MARKS,
    TYPE_LABELS,
    AssessmentConfig,
    Blueprint,
    BlueprintSection,
    BlueprintSlot,
    SectionConfig,
)

DIFFICULTY_PRESETS = {
    "easy": {"easy": 1.0, "medium": 0.0, "hard": 0.0},
    "medium": {"easy": 0.0, "medium": 1.0, "hard": 0.0},
    "hard": {"easy": 0.0, "medium": 0.0, "hard": 1.0},
    "mixed": {"easy": 0.3, "medium": 0.5, "hard": 0.2},
}


def largest_remainder(weights: dict[str, float], n: int) -> dict[str, int]:
    """Allocate n integer units proportionally to weights (Hamilton method, deterministic tie-break)."""
    total = sum(weights.values())
    if n <= 0 or total <= 0:
        return {k: 0 for k in weights}
    quotas = {k: n * w / total for k, w in weights.items()}
    alloc = {k: math.floor(q) for k, q in quotas.items()}
    rest = n - sum(alloc.values())
    for k in sorted(quotas, key=lambda k: (-(quotas[k] - alloc[k]), list(weights).index(k)))[:rest]:
        alloc[k] += 1
    return alloc


def resolve_sections(cfg: AssessmentConfig) -> list[SectionConfig]:
    if cfg.sections:
        return cfg.sections
    n = cfg.num_questions or 0
    types = cfg.question_types
    if n < len(types):
        raise ValidationFailed(f"{n} questions cannot cover {len(types)} question types; add questions or "
                               "remove types")
    alloc = largest_remainder({t: 1.0 for t in types}, n)
    return [SectionConfig(question_type=t, count=alloc[t]) for t in types if alloc[t] > 0]


def build_blueprint(assessment_id: str, cfg: AssessmentConfig, scope: list[dict]) -> Blueprint:
    """``scope``: [{"key","name"}] concepts eligible for questions (already filtered by coverage mode)."""
    if not scope:
        raise ValidationFailed("No eligible concepts: research and cover some material first, or switch to "
                               "'expanded' mode / select topics")
    sections_cfg = resolve_sections(cfg)
    sections: list[BlueprintSection] = []
    slots: list[BlueprintSlot] = []
    total = 0.0

    weights = {c["key"]: float(cfg.topic_distribution.get(c["key"], 1.0)) for c in scope}
    weights = {k: w for k, w in weights.items() if w > 0} or {c["key"]: 1.0 for c in scope}
    names = {c["key"]: c["name"] for c in scope}
    n_total = sum(s.count for s in sections_cfg)
    concept_alloc = largest_remainder(weights, n_total)
    concept_queue: list[str] = []
    # interleave concepts so each section gets a spread of topics
    remaining = dict(concept_alloc)
    while any(remaining.values()):
        for k in weights:
            if remaining.get(k):
                concept_queue.append(k)
                remaining[k] -= 1

    q_index = 0
    for si, sc in enumerate(sections_cfg):
        sid = sc.id or f"S{si + 1}"
        marks_each = sc.marks_each or cfg.marks_per_type.get(sc.question_type) or DEFAULT_MARKS[sc.question_type]
        level = sc.difficulty or cfg.difficulty
        dist = cfg.difficulty_distribution if (cfg.difficulty_distribution and level == "mixed") else \
            DIFFICULTY_PRESETS[level]
        diff_alloc = largest_remainder({k: float(dist.get(k, 0.0)) for k in ("easy", "medium", "hard")}, sc.count)
        diffs = [d for d in ("easy", "medium", "hard") for _ in range(diff_alloc[d])]
        title = sc.title or f"Section {chr(65 + si)}: {TYPE_LABELS[sc.question_type]}"
        sections.append(BlueprintSection(id=sid, title=title, question_type=sc.question_type,
                                         instructions=sc.instructions, count=sc.count, marks_each=marks_each,
                                         total=round(marks_each * sc.count, 2)))
        for qi in range(sc.count):
            ck = concept_queue[q_index % len(concept_queue)]
            q_index += 1
            slots.append(BlueprintSlot(
                slot_id=f"{sid}.{qi + 1}",
                section_id=sid, section_order=si, order_index=qi, type=sc.question_type, marks=marks_each,
                difficulty=diffs[qi] if qi < len(diffs) else "medium", concept_key=ck, concept_name=names[ck]))
        total += marks_each * sc.count

    total = round(total, 2)
    if cfg.total_marks is not None and abs(cfg.total_marks - total) > 1e-6:
        raise ValidationFailed(
            f"Configured total marks ({cfg.total_marks:g}) do not match sections ({total:g}). Adjust marks per "
            f"question or section counts.",
            details={"computed_total": total, "sections": [s.model_dump() for s in sections]})
    return Blueprint(sections=sections, slots=slots, total_marks=total, scope_concepts=scope,
                     coverage_mode=cfg.coverage_mode)
