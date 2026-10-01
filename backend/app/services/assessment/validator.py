"""Question validation: structure, answer keys, marks, numeric verification, duplicates.

``severity == "error"`` issues trigger the repair pass; anything still failing
afterwards (and every ``warning``) is attached to the question as a flag for
teacher review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.ids import normalize_key
from app.services.assessment.safe_math import UnsafeExpression, numbers_match, safe_eval
from app.services.assessment.schemas import RUBRIC_TYPES, BlueprintSlot, GenQuestion

_BLANK = re.compile(r"_{3,}|\[blank\]|\(blank\)", re.IGNORECASE)


@dataclass
class Issue:
    slot_id: str
    code: str
    message: str
    severity: str  # error | warning

    def as_flag(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


def validate_structure(q: GenQuestion, slot: BlueprintSlot) -> list[Issue]:
    issues: list[Issue] = []

    def err(code: str, msg: str) -> None:
        issues.append(Issue(slot.slot_id, code, msg, "error"))

    def warn(code: str, msg: str) -> None:
        issues.append(Issue(slot.slot_id, code, msg, "warning"))

    if q.type != slot.type:
        err("type_mismatch", f"Expected a {slot.type} question, got {q.type}")
        return issues

    if q.type == "mcq":
        choices = [c.strip() for c in q.choices if c and c.strip()]
        if not 3 <= len(choices) <= 6:
            err("mcq_choices", "MCQ must have 3-6 non-empty choices")
        elif len({normalize_key(c) for c in choices}) != len(choices):
            err("mcq_duplicate_choices", "MCQ choices must be distinct")
        if q.correct_choice is None or not (0 <= q.correct_choice < len(q.choices)):
            err("mcq_answer_key", "MCQ correct_choice must index one of the choices")
        elif q.answer and normalize_key(q.answer) not in (normalize_key(q.choices[q.correct_choice]),
                                                          chr(97 + q.correct_choice)):
            # tolerate letter answers ("b") but not contradicting text
            if normalize_key(q.answer) in {normalize_key(c) for c in q.choices}:
                err("mcq_answer_conflict", "Answer text does not match the correct_choice")
    elif q.type == "true_false":
        if normalize_key(q.answer) not in ("true", "false"):
            err("tf_answer", "True/false answer must be 'True' or 'False'")
    elif q.type == "fill_blank":
        if not _BLANK.search(q.text):
            err("fill_blank_marker", "Fill-in-the-blank text must contain a blank (e.g. '_____')")
        if not q.answer.strip():
            err("missing_answer", "Missing expected answer")
    elif q.type == "numerical":
        if q.numeric_answer is None:
            err("numeric_answer", "Numerical question needs numeric_answer")
        elif q.answer_expression:
            try:
                value = safe_eval(q.answer_expression)
                if not numbers_match(value, q.numeric_answer, q.tolerance):
                    err("numeric_mismatch", f"answer_expression evaluates to {value:g}, not {q.numeric_answer:g}")
            except (UnsafeExpression, ArithmeticError, ValueError, TypeError) as exc:
                warn("numeric_unverifiable", f"Could not verify the calculation automatically ({exc})")
        else:
            warn("numeric_unverified", "No calculation expression provided; answer not verified automatically")
    else:
        if not q.answer.strip():
            err("missing_answer", "Missing expected answer / model answer")

    if q.type in RUBRIC_TYPES:
        if not q.rubric:
            warn("missing_rubric", "No marking rubric provided")
    if q.type == "coding" and not (q.reference_solution and q.code_tests):
        warn("code_unverified", "No reference solution/tests; code cannot be verified automatically")
    if q.difficulty != slot.difficulty:
        warn("difficulty_mismatch", f"Blueprint asked for {slot.difficulty}; generator rated it {q.difficulty}")
    if not q.explanation.strip():
        warn("missing_explanation", "No explanation provided for the answer key")
    return issues


def normalized_rubric(q: GenQuestion, marks: float) -> tuple[list[dict], bool]:
    """Scale rubric marks so they sum to the question marks (deterministic repair). Returns (rubric, changed)."""
    if not q.rubric:
        return [], False
    total = sum(r.marks for r in q.rubric)
    if total <= 0:
        each = round(marks / len(q.rubric), 2)
        return [{"criterion": r.criterion, "marks": each} for r in q.rubric], True
    if abs(total - marks) < 1e-6:
        return [r.model_dump() for r in q.rubric], False
    scaled = [round(r.marks * marks / total, 2) for r in q.rubric]
    scaled[-1] = round(marks - sum(scaled[:-1]), 2)
    return [{"criterion": r.criterion, "marks": m} for r, m in zip(q.rubric, scaled, strict=True)], True


def canonical_answer(q: GenQuestion) -> dict:
    if q.type == "mcq" and q.correct_choice is not None and 0 <= q.correct_choice < len(q.choices):
        return {"choice_index": q.correct_choice, "choice_label": chr(65 + q.correct_choice),
                "text": q.choices[q.correct_choice]}
    if q.type == "true_false":
        return {"text": "True" if normalize_key(q.answer) == "true" else "False"}
    if q.type == "numerical":
        return {"value": q.numeric_answer, "tolerance": q.tolerance, "units": q.units, "text": q.answer or (
            f"{q.numeric_answer:g} {q.units or ''}".strip() if q.numeric_answer is not None else ""),
                "expression": q.answer_expression}
    if q.type == "coding":
        return {"text": q.answer, "language": q.code_language or "python",
                "reference_solution": q.reference_solution, "tests": q.code_tests}
    return {"text": q.answer}


def text_duplicates(texts: list[str]) -> list[tuple[int, int]]:
    seen: dict[str, int] = {}
    dups = []
    for i, t in enumerate(texts):
        k = normalize_key(t)
        if k in seen:
            dups.append((seen[k], i))
        else:
            seen[k] = i
    return dups


def vector_duplicates(vectors: list[list[float]], threshold: float) -> list[tuple[int, int, float]]:
    out = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            sim = sum(a * b for a, b in zip(vectors[i], vectors[j], strict=True))
            if sim >= threshold:
                out.append((i, j, sim))
    return out
