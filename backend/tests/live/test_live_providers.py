"""Live checks against Groq, Exa and Tavily. Each test is skipped unless its key is set.

Run with:  uv run pytest -m live tests/live
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.llm.citations import cited_markers
from app.llm.structured import generate_structured, untrusted
from app.providers.llm.groq import GroqLLMProvider
from app.providers.search.tavily import TavilySearchProvider
from app.services.research.agent import AnswerOutput, TurnPlan

pytestmark = [pytest.mark.live, pytest.mark.nodb]
S = Settings()

needs_groq = pytest.mark.skipif(not S.groq_api_key, reason="GROQ_API_KEY not set")
needs_tavily = pytest.mark.skipif(not S.tavily_api_key, reason="TAVILY_API_KEY not set")
needs_exa = pytest.mark.skipif(not S.exa_api_key, reason="EXA_API_KEY not set")

PASSAGES = {
    "quantum": "A qubit is represented using the Bloch sphere. Superposition enables quantum parallelism.",
    "history": "The Enlightenment influenced the French Revolution, which began in 1789 in Paris.",
    "iot": "A temperature sensor measures temperature and sends readings to an IoT gateway over MQTT.",
}


@needs_groq
@pytest.mark.parametrize("domain", list(PASSAGES))
async def test_groq_grounded_answer_cites_given_passages(domain):
    llm = GroqLLMProvider(S)
    system = ("Answer using the passages and cite them with markers like [S1]. Only use provided markers.\n"
              + untrusted("source", PASSAGES[domain], id="S1"))
    out = await generate_structured(llm, task="answer", tier="smart", schema=AnswerOutput, system=system,
                                    user="Explain the main idea of the passage in two sentences.")
    markers = cited_markers(out.answer_markdown) or out.cited_sources
    assert markers and set(markers) <= {"S1"}


@needs_groq
async def test_groq_turn_plan():
    plan = await generate_structured(GroqLLMProvider(S), task="plan_turn", system="Plan the turn.", tier="fast",
                                     schema=TurnPlan, user="Topic: Biology\nTeacher message:\nPlease research "
                                                            "how mitochondria produce ATP")
    assert plan.needs_external_research and plan.search_queries


@needs_tavily
async def test_tavily_search_returns_content():
    hits = await TavilySearchProvider(S).search("Bloch sphere qubit representation", max_results=3)
    assert hits and all(h.url.startswith("http") for h in hits)
    assert any(len(h.content) > 200 for h in hits)


@needs_exa
async def test_exa_search_returns_content():
    from app.providers.search.exa import ExaSearchProvider

    hits = await ExaSearchProvider(S).search("Bloch sphere qubit representation", max_results=3)
    assert hits and all(h.url.startswith("http") for h in hits)
    assert any(len(h.content) > 200 for h in hits)
