"""Live checks against Groq and Tavily. Skipped unless GROQ_API_KEY / TAVILY_API_KEY are set.

Run with:  uv run pytest -m live tests/live
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.llm.structured import generate_structured
from app.providers.llm.groq import GroqLLMProvider
from app.providers.search.tavily import TavilySearchProvider
from app.services.knowledge.extractor import Passage, extract_candidates
from app.services.knowledge.ontology import OntologyResolver, PassageRef
from app.services.knowledge.relations import RelationRegistry
from app.services.research.agent import TurnPlan

pytestmark = [pytest.mark.live, pytest.mark.nodb]
S = Settings()

needs_groq = pytest.mark.skipif(not S.groq_api_key, reason="GROQ_API_KEY not set")
needs_tavily = pytest.mark.skipif(not S.tavily_api_key, reason="TAVILY_API_KEY not set")
needs_exa = pytest.mark.skipif(not S.exa_api_key, reason="EXA_API_KEY not set")


@needs_groq
@pytest.mark.parametrize("text", [
    "A qubit is represented using the Bloch sphere. Superposition enables quantum parallelism.",
    "The Enlightenment influenced the French Revolution, which began in 1789 in Paris.",
    "A temperature sensor measures temperature and sends readings to an IoT gateway over MQTT.",
])
async def test_groq_extraction_resolves_cleanly(text):
    llm = GroqLLMProvider(S)
    result = await extract_candidates(llm, topic="test", passages=[Passage("P1", "c1", "s1", "src", text)])
    assert result.entities and (result.relations or result.facts)
    batch = OntologyResolver("ws-live", RelationRegistry.default(), []).resolve(
        result, {"P1": PassageRef("c1", "s1", text)}, extraction_id="e", session_id=None, extracted_at="now")
    assert batch.claims
    assert any(ev.quote_verified for ev in batch.evidence), "model quotes should match the passage verbatim"


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
