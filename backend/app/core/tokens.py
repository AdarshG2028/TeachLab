"""Approximate token counting.

Groq-hosted Llama models use a different tokenizer than tiktoken's cl100k; we
use cl100k as an approximation and callers apply a safety margin
(``context_budget_ratio``). Falls back to a character heuristic if the
encoding cannot be loaded (e.g. offline first run).
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _encoding():  # pragma: no cover - trivial
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _encoding()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text, disallowed_special=()))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    enc = _encoding()
    if enc is None:
        return text[: max_tokens * 4]
    ids = enc.encode(text, disallowed_special=())
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[:max_tokens])
