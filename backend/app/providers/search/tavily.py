"""Tavily search + extract provider."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderError
from app.core.retry import with_retries
from app.providers.search.base import ExtractedPage, SearchHit, SearchProvider

TAVILY_API = "https://api.tavily.com"


class TavilySearchProvider(SearchProvider):
    """Uses Tavily's REST API directly (small surface; keeps error handling explicit)."""

    name = "tavily"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.tavily_api_key and client is None:
            raise ConfigurationError("TAVILY_API_KEY is not set (or set SEARCH_PROVIDER=fixture for tests)")
        self.api_key = settings.tavily_api_key or ""
        self.client = client or httpx.AsyncClient(base_url=TAVILY_API, timeout=40.0)
        self.retries = settings.llm_max_retries

    async def _post(self, path: str, body: dict[str, Any], op: str) -> dict[str, Any]:
        async def _call() -> dict[str, Any]:
            try:
                resp = await self.client.post(path, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ProviderError(f"Tavily connection problem: {exc}", retryable=True) from exc
            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                raise ProviderError("Tavily rate limited", retryable=True,
                                    retry_after=float(retry_after) if retry_after else None)
            if resp.status_code in (401, 403):
                raise ProviderError("Tavily rejected the API key", retryable=False)
            if resp.status_code == 432 or resp.status_code == 433:
                raise ProviderError("Tavily usage/plan limit exceeded", retryable=False)
            if resp.status_code >= 500:
                raise ProviderError(f"Tavily server error {resp.status_code}", retryable=True)
            if resp.status_code >= 400:
                raise ProviderError(f"Tavily error {resp.status_code}: {resp.text[:300]}", retryable=False)
            return resp.json()

        return await with_retries(_call, attempts=self.retries, op=op)

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        data = await self._post(
            "/search",
            {
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_raw_content": True,
                "include_answer": False,
            },
            op="tavily:search",
        )
        hits: list[SearchHit] = []
        for r in data.get("results", []):
            if not r.get("url"):
                continue
            hits.append(
                SearchHit(
                    url=r["url"],
                    title=r.get("title") or "",
                    snippet=r.get("content") or "",
                    content=r.get("raw_content") or "",
                    published_date=r.get("published_date"),
                    score=float(r.get("score") or 0.0),
                )
            )
        return hits

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        if not urls:
            return []
        data = await self._post("/extract", {"urls": urls[:20]}, op="tavily:extract")
        return [
            ExtractedPage(url=r["url"], content=r.get("raw_content") or "", title=r.get("title") or "")
            for r in data.get("results", [])
            if r.get("url") and r.get("raw_content")
        ]
