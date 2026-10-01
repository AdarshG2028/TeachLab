"""LLM-based candidate extraction of entities, relations and facts from passages."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.tokens import count_tokens
from app.llm.structured import UNTRUSTED_POLICY, generate_structured, untrusted
from app.providers.llm.base import LLMProvider
from app.services.knowledge.schemas import ExtractionResult

PROMPT_VERSION = "x1"

SYSTEM = f"""You extract a knowledge graph from research passages for a teacher preparing to teach a topic.
Return only knowledge that is explicitly stated in the passages.

Rules:
- entities: important concepts, people, events, places, organizations, technologies and methods relevant to the topic.
  category is one of Concept, Person, Event, Place, Organization, Technology, Method, Other.
  Put acronyms and alternative names in aliases. Keep names short and canonical (e.g. "Qubit", "French Revolution").
- relations: subject -> predicate -> object, where predicate is a short natural verb phrase as written or implied
  in the passage (e.g. "is represented using", "measures", "influenced", "is a type of", "consists of").
  Do not invent relation types beyond what the text supports.
- facts: important standalone statements about one entity that are not relations (definitions, properties, dates).
- passage: the id (e.g. "P2") of the passage that supports the item.
- quote: copy a short supporting span VERBATIM from that passage (max ~30 words). Never paraphrase in quote.
- negated: true only if the passage explicitly denies the relation.
- perspective: if the passage presents something as a viewpoint, interpretation or disputed claim, record whose view
  it is (e.g. "according to revisionist historians"). Leave null for uncontested statements.
- time_context: dates or periods the statement applies to, if given.
- confidence: your heuristic estimate (0-1) that the passage really supports the item.
Prefer 5-25 high-value items per batch over exhaustive trivia.

{UNTRUSTED_POLICY}"""


@dataclass
class Passage:
    pid: str  # "P1"...
    chunk_id: str
    source_id: str
    source_title: str
    text: str


def batch_passages(passages: list[Passage], max_tokens: int) -> list[list[Passage]]:
    batches: list[list[Passage]] = []
    cur: list[Passage] = []
    used = 0
    for p in passages:
        t = count_tokens(p.text) + 30
        if cur and used + t > max_tokens:
            batches.append(cur)
            cur, used = [], 0
        cur.append(p)
        used += t
    if cur:
        batches.append(cur)
    return batches


async def extract_candidates(llm: LLMProvider, *, topic: str, passages: list[Passage]) -> ExtractionResult:
    body = "\n\n".join(untrusted("passage", p.text, id=p.pid, source=p.source_title[:120]) for p in passages)
    user = (f"Workspace topic: {topic}\n"
            f"Passage ids available: {', '.join(p.pid for p in passages)}\n\n{body}\n\n"
            "Extract entities, relations and facts as JSON.")
    return await generate_structured(llm, task="extract", system=SYSTEM, user=user, schema=ExtractionResult,
                                     tier="fast", max_tokens=3000, temperature=0.1)
