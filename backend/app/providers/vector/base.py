"""Vector store interface. Every operation is scoped to a workspace."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VectorPoint:
    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorHit:
    id: str
    score: float
    payload: dict[str, Any]


class VectorStore(ABC):
    @abstractmethod
    async def ensure_collection(self) -> None: ...

    @abstractmethod
    async def upsert(self, workspace_id: str, points: list[VectorPoint]) -> None: ...

    @abstractmethod
    async def search(
        self,
        workspace_id: str,
        vector: list[float],
        *,
        top_k: int = 8,
        source_ids: list[str] | None = None,
        kind: str | None = None,
    ) -> list[VectorHit]: ...

    @abstractmethod
    async def delete_source(self, workspace_id: str, source_id: str) -> None: ...

    @abstractmethod
    async def delete_workspace(self, workspace_id: str) -> None: ...

    @abstractmethod
    async def count(self, workspace_id: str) -> int: ...
