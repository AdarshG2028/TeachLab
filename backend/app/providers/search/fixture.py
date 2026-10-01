"""Deterministic offline search provider backed by JSON fixture files.

Only permitted when ENV is development or test (see Settings). Each fixture file:
``{"keywords": ["qubit", ...], "documents": [{"url", "title", "content", "published_date"}]}``.
A query matches a fixture when it shares keywords; documents are ranked by term overlap.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.providers.search.base import ExtractedPage, SearchHit, SearchProvider

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


class FixtureSearchProvider(SearchProvider):
    name = "fixture"

    def __init__(self, fixture_dir: Path) -> None:
        self.documents: list[dict] = []
        self.keywords: list[tuple[set[str], list[dict]]] = []
        for path in sorted(Path(fixture_dir).glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            docs = data.get("documents", [])
            self.documents.extend(docs)
            self.keywords.append(({k.lower() for k in data.get("keywords", [])}, docs))
        self.calls: list[str] = []

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        self.calls.append(query)
        q = _terms(query)
        scored: list[tuple[float, dict]] = []
        for keywords, docs in self.keywords:
            kw_overlap = len(q & keywords)
            if not kw_overlap:
                continue
            for d in docs:
                overlap = len(q & _terms(d["title"] + " " + d["content"][:2000]))
                scored.append((kw_overlap * 2 + overlap, d))
        scored.sort(key=lambda x: -x[0])
        return [
            SearchHit(url=d["url"], title=d["title"], snippet=d["content"][:300], content=d["content"],
                      published_date=d.get("published_date"), score=min(1.0, s / 20))
            for s, d in scored[:max_results]
        ]

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        by_url = {d["url"]: d for d in self.documents}
        return [ExtractedPage(url=u, content=by_url[u]["content"], title=by_url[u]["title"]) for u in urls if u in by_url]
