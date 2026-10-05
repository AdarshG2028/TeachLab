"""Concept-name normalization and matching.

Concepts (roadmap items, coverage records, test topics) are identified by a stable key derived from
their normalized name, so "Qubits", "the qubit" and "Qubit" are the same concept within a workspace.
"""

from __future__ import annotations

import re

from app.core.ids import normalize_key, stable_id

_NON_PLURAL_ENDINGS = ("ss", "us", "is", "ics", "sis", "ous", "as")
_STOP = {"and", "the", "for", "with", "from", "into", "of", "to", "in", "on", "a", "an", "its", "their", "how",
         "what", "why", "basic", "basics", "introduction", "intro", "overview", "using", "use", "via", "vs"}


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


def concept_name_keys(name: str) -> list[str]:
    """Normalized match keys for a concept name: as written, singular, without a leading article."""
    base = normalize_key(name)
    if not base:
        return []
    words = base.split(" ")
    singular = " ".join([*words[:-1], singularize(words[-1])])
    no_article = re.sub(r"^(the|a|an) ", "", singular)
    return list(dict.fromkeys(k for k in (base, singular, no_article) if k))


def concept_key(workspace_id: str, name: str) -> str:
    keys = concept_name_keys(name)
    return stable_id(workspace_id, "concept", keys[-1] if keys else normalize_key(name))


def _stem(word: str) -> str:
    if word.endswith(("ss", "us", "is")):
        return word
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def concept_terms(concept: str) -> list[str]:
    """Content words of a concept name, lightly stemmed ("Scoped atomics in Rust" -> scop, atomic, rust)."""
    words = re.findall(r"[a-z0-9_#+]+", concept.lower())
    return list(dict.fromkeys(_stem(w) for w in words if w not in _STOP and len(w) >= 3))


def term_overlap(terms: list[str], text: str) -> float:
    """Fraction of concept terms whose stem starts a word in the text (borrowing ~ borrows ~ borrowed)."""
    if not terms:
        return 0.0
    low = text.lower()
    return sum(1 for t in terms if re.search(rf"(?<![a-z0-9_]){re.escape(t)}", low)) / len(terms)
