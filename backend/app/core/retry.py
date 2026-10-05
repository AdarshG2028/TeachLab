"""Bounded retry with exponential backoff and jitter for provider calls."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.errors import ProviderError
from app.core.logging import get_logger

T = TypeVar("T")
log = get_logger(__name__)


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    op: str,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Retry ``fn`` on retryable ProviderErrors, honoring ``retry_after`` hints."""
    sleep = sleep or asyncio.sleep
    last: ProviderError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt == attempts:
                raise
            delay = exc.retry_after if exc.retry_after is not None else min(max_delay, base_delay * 2 ** (attempt - 1))
            delay = min(max_delay, delay) + random.uniform(0, 0.25 * delay)
            log.warning("provider_retry", op=op, attempt=attempt, delay=round(delay, 2), error=exc.message)
            await sleep(delay)
    assert last is not None  # unreachable
    raise last


class TokenBucket:
    """Simple per-process tokens-per-minute limiter (Groq free tier enforces TPM)."""

    def __init__(self, tokens_per_minute: int, clock: Callable[[], float] | None = None) -> None:
        self.capacity = max(1, tokens_per_minute)
        self.tokens = float(self.capacity)
        self.rate = self.capacity / 60.0
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self, amount: int, *, low_priority: bool = False) -> None:
        """Wait until ``amount`` tokens are available.

        Low-priority callers (background extraction/summaries) additionally leave a reserve of ~35% of the
        per-minute budget untouched, so interactive requests (chat answers, lessons) are not starved.
        """
        amount = min(amount, self.capacity)
        needed = min(self.capacity, amount + (int(self.capacity * 0.35) if low_priority else 0))
        while True:
            async with self._lock:
                now = self._clock()
                if self._last is not None:
                    self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= needed:
                    self.tokens -= amount
                    return
                wait = (needed - self.tokens) / self.rate
            # sleep outside the lock so a high-priority caller can take tokens meanwhile
            await asyncio.sleep(min(wait, 5.0))
