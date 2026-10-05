"""Exa search + contents provider (https://exa.ai/docs/reference/search)."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderError
from app.core.retry import with_retries
from app.providers.search.base import ExtractedPage, SearchHit, SearchProvider

EXA_API = "https://api.exa.ai"
MAX_TEXT_CHARS = 20_000  # per result; keeps ingestion and token use bounded


class ExaSearchProvider(SearchProvider):
    name = "exa"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.exa_api_key and client is None:
            raise ConfigurationError("EXA_API_KEY is not set (or choose another SEARCH_PROVIDER)")
        self.api_key = settings.exa_api_key or ""
        self.client = client or httpx.AsyncClient(base_url=EXA_API, timeout=60.0)
        self.retries = settings.llm_max_retries

    async def _post(self, path: str, body: dict[str, Any], op: str) -> dict[str, Any]:
        async def _call() -> dict[str, Any]:
            try:
                resp = await self.client.post(path, json=body, headers={"x-api-key": self.api_key})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ProviderError(f"Exa connection problem: {exc}", retryable=True) from exc
            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                raise ProviderError("Exa rate limited", retryable=True,
                                    retry_after=float(retry_after) if retry_after else None)
            if resp.status_code in (401, 403):
                raise ProviderError("Exa rejected the API key (check EXA_API_KEY)", retryable=False)
            if resp.status_code == 402:
                raise ProviderError("Exa account is out of credits", retryable=False)
            if resp.status_code >= 500:
                raise ProviderError(f"Exa server error {resp.status_code}", retryable=True)
            if resp.status_code >= 400:
                raise ProviderError(f"Exa error {resp.status_code}: {resp.text[:300]}", retryable=False)
            return resp.json()

        return await with_retries(_call, attempts=self.retries, op=op)

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        data = await self._post(
            "/search",
            {"query": query, "numResults": max_results, "type": "auto",
             "contents": {"text": {"maxCharacters": MAX_TEXT_CHARS}}},
            op="exa:search",
        )
        results = [r for r in data.get("results", []) if r.get("url")]
        n = max(1, len(results))
        return [
            SearchHit(
                url=r["url"],
                title=r.get("title") or "",
                snippet=(r.get("highlights") or [""])[0] or (r.get("text") or "")[:300],
                content=r.get("text") or "",
                published_date=r.get("publishedDate"),
                score=1.0 - i / n,  # Exa returns results in relevance order without a score
            )
            for i, r in enumerate(results)
        ]

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        if not urls:
            return []
        data = await self._post("/contents", {"urls": urls[:100], "text": {"maxCharacters": MAX_TEXT_CHARS}},
                                op="exa:contents")
        return [
            ExtractedPage(url=r["url"], content=r.get("text") or "", title=r.get("title") or "")
            for r in data.get("results", [])
            if r.get("url") and r.get("text")
        ]
