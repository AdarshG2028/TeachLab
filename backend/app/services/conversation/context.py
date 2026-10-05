"""Token-budgeted LLM context construction.

Each request is assembled from: the current request, recent messages, the
rolling session summary, structured session state, explicit preferences,
and retrieved source passages. The full history stays in
PostgreSQL; only a budgeted slice is sent to the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.core.tokens import count_tokens, truncate_to_tokens
from app.llm.structured import untrusted


@dataclass
class ContextInputs:
    system: str
    user_message: str
    summary: str = ""
    session_state: dict = field(default_factory=dict)
    preferences: list[dict] = field(default_factory=list)
    passages: list[dict] = field(default_factory=list)  # {"marker","title","url","text","score"}
    recent_messages: list[dict] = field(default_factory=list)  # oldest -> newest {"role","content"}


@dataclass
class Budget:
    total: int
    summary: int
    state: int
    preferences: int
    passages: int
    history: int


def make_budget(context_tokens: int, max_output: int, ratio: float) -> Budget:
    usable = int(context_tokens * ratio) - max_output
    usable = max(usable, 2_000)
    return Budget(
        total=usable,
        summary=min(900, usable // 10),
        state=min(500, usable // 16),
        preferences=min(250, usable // 30),
        passages=int(usable * 0.50),
        history=int(usable * 0.25),
    )


@dataclass
class BuiltContext:
    messages: list[dict]
    used_tokens: int
    included_passages: list[str]
    included_history: int
    dropped_passages: int
    dropped_history: int


def _fit_list(items: list[str], budget: int) -> tuple[list[str], int]:
    out, used = [], 0
    for it in items:
        t = count_tokens(it)
        if used + t > budget:
            break
        out.append(it)
        used += t
    return out, used


def build_context(inp: ContextInputs, budget: Budget) -> BuiltContext:
    sections: list[str] = []
    used = count_tokens(inp.system) + count_tokens(inp.user_message)

    if inp.summary:
        s = truncate_to_tokens(inp.summary, budget.summary)
        sections.append(f"## Conversation summary (older messages)\n{s}")
        used += count_tokens(s)
    if inp.session_state:
        st = truncate_to_tokens(json.dumps(inp.session_state, ensure_ascii=False, default=str), budget.state)
        sections.append(f"## Session state\n{st}")
        used += count_tokens(st)
    if inp.preferences:
        lines = [f"- {p['key']}: {json.dumps(p['value'], ensure_ascii=False)} ({p.get('origin', 'explicit')})"
                 for p in inp.preferences]
        kept, t = _fit_list(lines, budget.preferences)
        sections.append("## Teacher preferences\n" + "\n".join(kept))
        used += t
    passage_blocks = [untrusted("source", p["text"], id=p["marker"], title=p.get("title", "")[:150],
                                url=p.get("url") or "") for p in inp.passages]
    kept_p, t = _fit_list(passage_blocks, budget.passages)
    if kept_p:
        sections.append("## Retrieved source passages\n" + "\n\n".join(kept_p))
    used += t
    included_markers = [p["marker"] for p in inp.passages[: len(kept_p)]]

    # history: newest first until the budget is spent, then restore chronological order
    remaining = max(0, min(budget.history, budget.total - used))
    hist: list[dict] = []
    h_used = 0
    for m in reversed(inp.recent_messages):
        t = count_tokens(m["content"]) + 4
        if hist and h_used + t > remaining:
            break
        if not hist and t > remaining:  # always keep a truncated version of the latest turn
            hist.append({"role": m["role"], "content": truncate_to_tokens(m["content"], max(50, remaining - 4))})
            h_used += remaining
            break
        hist.append({"role": m["role"], "content": m["content"]})
        h_used += t
    hist.reverse()
    used += h_used

    system = inp.system + ("\n\n" + "\n\n".join(sections) if sections else "")
    messages = [{"role": "system", "content": system}, *hist, {"role": "user", "content": inp.user_message}]
    return BuiltContext(messages=messages, used_tokens=used, included_passages=included_markers,
                        included_history=len(hist), dropped_passages=len(inp.passages) - len(kept_p),
                        dropped_history=len(inp.recent_messages) - len(hist))
