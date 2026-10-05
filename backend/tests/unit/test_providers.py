"""Groq, Tavily and Exa providers against mocked HTTP: request shape, error mapping, retries."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderError
from app.llm.structured import generate_structured
from app.providers.llm.base import ChatMessage
from app.providers.llm.groq import GroqLLMProvider
from app.providers.search.exa import ExaSearchProvider
from app.providers.search.tavily import TavilySearchProvider
from app.services.knowledge.schemas import ExtractionResult

pytestmark = pytest.mark.nodb


def settings(**kw) -> Settings:
    return Settings(env="test", groq_api_key="test-key", tavily_api_key="tv-key", llm_max_retries=3,
                    llm_tokens_per_minute=1_000_000, **kw)


def groq_with(handler) -> GroqLLMProvider:
    client = AsyncOpenAI(api_key="k", base_url="https://api.groq.test/openai/v1", max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return GroqLLMProvider(settings(), client=client)


def completion(content: str) -> dict:
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "llama-3.1-8b-instant",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


async def test_groq_request_shape_and_tier_routing():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=completion('{"entities": [], "relations": [], "facts": []}'))

    llm = groq_with(handler)
    out = await generate_structured(llm, task="extract", system="s", user="u", schema=ExtractionResult, tier="fast")
    assert out.entities == []
    body = seen[0]
    assert body["model"] == "openai/gpt-oss-20b" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system" and "JSON Schema" in body["messages"][0]["content"]
    await llm.complete([ChatMessage("user", "hi")], task="answer", tier="smart")
    assert seen[1]["model"] == "openai/gpt-oss-120b" and "response_format" not in seen[1]
    assert seen[1]["reasoning_effort"] == "low"  # reasoning models get a small reasoning budget


async def test_groq_rate_limit_is_retried_with_retry_after(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr("app.core.retry.asyncio.sleep", fake_sleep)

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, json=completion("ok"))

    res = await groq_with(handler).complete([ChatMessage("user", "x")], task="t")
    assert res.text == "ok" and calls["n"] == 2 and sleeps and sleeps[0] >= 3


async def test_groq_auth_error_not_retried():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    with pytest.raises(ProviderError) as e:
        await groq_with(handler).complete([ChatMessage("user", "x")], task="t")
    assert calls["n"] == 1 and not e.value.retryable


def test_groq_requires_key():
    with pytest.raises(ConfigurationError):
        GroqLLMProvider(Settings(env="test", groq_api_key=None))


async def test_tavily_search_and_extract(monkeypatch):
    async def no_sleep(d):
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    bodies = []
    state = {"search_calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tv-key"
        body = json.loads(req.content)
        bodies.append((req.url.path, body))
        if req.url.path == "/search":
            state["search_calls"] += 1
            if state["search_calls"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"results": [
                {"url": "https://a.edu/x", "title": "A", "content": "snippet", "raw_content": "full text",
                 "score": 0.9, "published_date": "2024-01-01"},
                {"title": "no url"}]})
        return httpx.Response(200, json={"results": [{"url": "https://a.edu/x", "raw_content": "page"}],
                                         "failed_results": []})

    client = httpx.AsyncClient(base_url="https://api.tavily.com", transport=httpx.MockTransport(handler))
    t = TavilySearchProvider(settings(), client=client)
    hits = await t.search("qubits", max_results=3)
    assert len(hits) == 1 and hits[0].content == "full text" and hits[0].published_date == "2024-01-01"
    assert bodies[-1][1]["include_raw_content"] is True and bodies[-1][1]["max_results"] == 3
    pages = await t.extract(["https://a.edu/x"])
    assert pages[0].content == "page"


async def test_tavily_quota_error_is_not_retried():
    def handler(req):
        return httpx.Response(432, json={"detail": "plan limit"})

    client = httpx.AsyncClient(base_url="https://api.tavily.com", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as e:
        await TavilySearchProvider(settings(), client=client).search("x")
    assert not e.value.retryable


def test_production_guards():
    with pytest.raises(ValueError):
        Settings(env="production", auth_provider="local")
    with pytest.raises(ValueError):
        Settings(env="production", auth_provider="clerk", llm_provider="fake")


async def test_code_runner_pass_fail_timeout():
    from app.services.assessment.code_runner import run_python_tests

    ok = await run_python_tests("def add(a, b):\n    return a + b", "assert add(2, 3) == 5", timeout=20)
    assert ok.passed
    bad = await run_python_tests("def add(a, b):\n    return a - b", "assert add(2, 3) == 5", timeout=20)
    assert not bad.passed and "AssertionError" in bad.output
    slow = await run_python_tests("while True:\n    pass", "", timeout=1.5)
    assert slow.timed_out and not slow.passed


def small_groq(handler, tpm: int) -> GroqLLMProvider:
    client = AsyncOpenAI(api_key="k", base_url="https://api.groq.test/openai/v1", max_retries=0,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return GroqLLMProvider(Settings(env="test", groq_api_key="k", llm_tokens_per_minute=tpm, llm_max_retries=2),
                           client=client)


async def test_request_is_capped_to_tokens_per_minute():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=completion("ok"))

    llm = small_groq(handler, tpm=3000)
    assert llm.context_tokens("smart") == 3000  # budgets downstream shrink accordingly
    await llm.complete([ChatMessage("user", "word " * 1000)], task="t", max_tokens=4000)
    sent = seen[0]
    assert sent["max_tokens"] <= 3000 - 1000  # output shrunk so prompt + output fit one request
    with pytest.raises(ProviderError) as e:
        await llm.complete([ChatMessage("user", "word " * 2900)], task="t", max_tokens=1000)
    assert "LLM_TOKENS_PER_MINUTE" in e.value.message and not e.value.retryable
    assert len(seen) == 1  # the oversized request was never sent


async def test_groq_request_too_large_error_is_explained():
    def handler(req):
        return httpx.Response(413, json={"error": {"message": "Request too large for model: Limit 6000, Requested 9000",
                                                   "type": "tokens", "code": "rate_limit_exceeded"}})

    with pytest.raises(ProviderError) as e:
        await small_groq(handler, tpm=50_000).complete([ChatMessage("user", "x")], task="t")
    assert "LLM_TOKENS_PER_MINUTE" in e.value.message and not e.value.retryable


def test_budget_helpers_shrink_with_small_caps():
    from app.core.tokens import count_tokens, fit_items
    from app.llm.structured import output_budget, prompt_budget

    llm = small_groq(lambda r: httpx.Response(200, json=completion("ok")), tpm=6000)
    assert output_budget(llm, "smart", 6000) == 3000
    assert prompt_budget(llm, "smart", 3000, overhead=1500) == 600
    items = ["passage text " * 100] * 10
    kept = fit_items(items, 900)
    assert sum(count_tokens(i) for i in kept) <= 900 and len(kept) >= 1
    assert fit_items(["x " * 5000], 200) and count_tokens(fit_items(["x " * 5000], 200)[0]) <= 200


async def test_token_bucket_clamps_oversized_amounts():
    from app.core.retry import TokenBucket

    bucket = TokenBucket(100, clock=lambda: 0.0)
    await asyncio.wait_for(bucket.acquire(10_000), timeout=1)  # clamped to capacity, does not deadlock
    assert bucket.tokens == 0


def exa_with(handler) -> ExaSearchProvider:

    client = httpx.AsyncClient(base_url="https://api.exa.ai", transport=httpx.MockTransport(handler))
    return ExaSearchProvider(Settings(env="test", exa_api_key="exa-key", llm_max_retries=3), client=client)


async def test_exa_search_and_contents(monkeypatch):
    async def no_sleep(d):
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    seen = []
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-api-key"] == "exa-key"
        body = json.loads(req.content)
        seen.append((req.url.path, body))
        if req.url.path == "/search":
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "1"})
            return httpx.Response(200, json={"requestId": "r", "results": [
                {"url": "https://a.edu/q", "title": "Qubits", "publishedDate": "2024-02-01T00:00:00.000Z",
                 "text": "A qubit is represented using the Bloch sphere. " * 10},
                {"url": "https://b.org/x", "title": "B", "text": "short", "highlights": ["key snippet"]},
                {"title": "no url"}]})
        return httpx.Response(200, json={"results": [{"url": "https://a.edu/q", "title": "Q", "text": "page text"},
                                                     {"url": "https://bad.example", "text": ""}],
                                         "statuses": [{"id": "https://bad.example", "status": "error"}]})

    exa = exa_with(handler)
    hits = await exa.search("qubit bloch sphere", max_results=3)
    assert [h.url for h in hits] == ["https://a.edu/q", "https://b.org/x"]
    assert hits[0].score > hits[1].score and "Bloch sphere" in hits[0].content
    assert hits[0].published_date.startswith("2024-02-01") and hits[1].snippet == "key snippet"
    body = seen[-1][1]
    assert body["numResults"] == 3 and body["contents"]["text"]["maxCharacters"] > 0
    pages = await exa.extract(["https://a.edu/q", "https://bad.example"])
    assert [p.url for p in pages] == ["https://a.edu/q"]


@pytest.mark.parametrize("status,retryable", [(401, False), (402, False), (503, True)])
async def test_exa_error_mapping(monkeypatch, status, retryable):
    async def no_sleep(d):
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    with pytest.raises(ProviderError) as e:
        await exa_with(lambda req: httpx.Response(status, json={"error": "x"})).search("x")
    assert e.value.retryable is retryable


def test_container_selects_exa(tmp_path):
    from app.container import Container

    c = Container(Settings(env="test", search_provider="exa", exa_api_key="k", storage_dir=tmp_path))
    assert isinstance(c.search, ExaSearchProvider)
