"""LLM provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

Tier = Literal["fast", "smart"]
Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class LLMResult:
    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)


class LLMProvider(ABC):
    """Chat-completion provider.

    ``task`` is a short stable name for the call site (e.g. ``"extract"``). Real
    providers use it only for logging/metrics; the deterministic test provider
    dispatches on it.
    """

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        task: str,
        tier: Tier = "smart",
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult: ...

    @abstractmethod
    def model_for(self, tier: Tier) -> str: ...

    @abstractmethod
    def context_tokens(self, tier: Tier) -> int: ...
