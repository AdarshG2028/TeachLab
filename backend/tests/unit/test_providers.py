"""Groq and Tavily providers against mocked HTTP: request shape, error mapping, retries."""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderError
from app.llm.structured import generate_structured
from app.providers.llm.base import ChatMessage
from app.providers.llm.groq import GroqLLMProvider
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
    assert body["model"] == "llama-3.1-8b-instant" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system" and "JSON Schema" in body["messages"][0]["content"]
    await llm.complete([ChatMessage("user", "hi")], task="answer", tier="smart")
    assert seen[1]["model"] == "llama-3.3-70b-versatile" and "response_format" not in seen[1]


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
