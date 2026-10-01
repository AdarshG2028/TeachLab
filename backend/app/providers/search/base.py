"""Web search provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class SearchHit(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    content: str = ""  # full extracted text when the provider returns it
    published_date: str | None = None
    score: float = 0.0


class ExtractedPage(BaseModel):
    url: str
    content: str
    title: str = ""


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]: ...

    @abstractmethod
    async def extract(self, urls: list[str]) -> list[ExtractedPage]: ...
