"""Deterministic LLM stand-in for tests and offline demos (ENV=development/test only).

It derives its outputs from the prompt content (passages, slots, concept lists)
using simple rules, so the full pipeline (extraction -> ontology resolution ->
graph -> roadmap -> notes -> assessment -> PDF) runs end-to-end without network
access. It validates orchestration, validation and persistence; it says nothing
about real model quality. Tests can override any task with ``script()``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from app.providers.llm.base import ChatMessage, LLMProvider, LLMResult, LLMUsage, Tier

_TAG = r'<untrusted_{label} ([^>]*)>\n(.*?)\n</untrusted_{label}>'
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_SENT = re.compile(r"(?<=[.!?])\s+")
_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)


def _tags(label: str, text: str) -> list[tuple[dict[str, str], str]]:
    out = []
    for m in re.finditer(_TAG.format(label=label), text, re.S):
        out.append((dict(_ATTR.findall(m.group(1))), m.group(2)))
    return out


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):  # headings are not sentences
            continue
        out.extend(s.strip() for s in _SENT.split(line.strip("*- ")) if len(s.strip()) > 20)
    return out


def _relation_phrases() -> list[str]:
    from app.services.knowledge.relations import CANONICAL

    phrases = set()
    for spec in CANONICAL:
        phrases.update(spec.synonyms)
        phrases.update(spec.reverse_synonyms)
    # a few phrases the registry does not know, to exercise new-relation-type proposals
    phrases.update({"orbits", "is encrypted with", "is governed by laws of"})
    return sorted((p for p in phrases if len(p) > 3), key=len, reverse=True)


_PHRASES: list[str] | None = None


def _clean_np(text: str, max_words: int = 6) -> str:
    text = re.split(r"[,;:]| which | that | while | because | when | for | in order", text)[0]
    words = text.strip().split()
    out = _ARTICLE.sub("", " ".join(words[:max_words])).strip(" .")
    out = re.sub(r"\s+(is|are|was|were|has|have|had|can|be)$", "", out, flags=re.I)
    return re.sub(r"^(each|every|some|all)\s+", "", out, flags=re.I).strip()


def _category(name: str, context: str) -> str:
    n = name.lower()
    if any(w in n for w in ("revolution", "war", "treaty", "election", "crisis", "reign", "battle")):
        return "Event"
    if any(w in n for w in ("france", "paris", "europe", "america", "england", "city", "region")):
        return "Place"
    if any(w in n for w in ("company", "university", "aws", "amazon", "google", "microsoft", "ibm", "assembly",
                            "estates")):
        return "Organization"
    if re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", name) and re.search(
            rf"(by|from) {re.escape(name)}", context):
        return "Person"
    if any(w in n for w in ("algorithm", "method", "protocol", "technique", "gate", "encryption")):
        return "Method"
    if any(w in n for w in ("sensor", "computer", "lambda", "server", "device", "processor", "network")):
        return "Technology"
    return "Concept"


def extract_rules(passages: list[tuple[dict, str]]) -> dict[str, Any]:
    global _PHRASES
    if _PHRASES is None:
        _PHRASES = _relation_phrases()
    entities: dict[str, dict] = {}
    relations, facts = [], []
    for attrs, text in passages:
        pid = attrs.get("id", "P1")
        for sent in _sentences(text):
            low = sent.lower()
            hit = None
            for ph in _PHRASES:
                m = re.search(rf"\s(does not|did not|do not|is not|was not)?\s?{re.escape(ph)}\s", low)
                if m and m.start() > 2:
                    hit = (ph, m)
                    break
            if not hit:
                if re.search(r"\b(is defined as|refers to|is known as)\b", low):
                    about = _clean_np(re.split(r"\b(is defined as|refers to|is known as)\b", sent)[0])
                    if about:
                        entities.setdefault(about.lower(), {"name": about, "category": _category(about, text)})
                        facts.append({"about": about, "statement": sent, "passage": pid, "quote": sent,
                                      "confidence": 0.7})
                continue
            ph, m = hit
            subj = _clean_np(sent[: m.start()])
            obj = _clean_np(sent[m.end():])
            if not subj or not obj or len(subj) > 80:
                continue
            negated = bool(m.group(1))
            for name in (subj, obj):
                entities.setdefault(name.lower(), {"name": name, "category": _category(name, text)})
            relations.append({"subject": subj, "predicate": ph, "object": obj, "passage": pid, "quote": sent,
                              "negated": negated, "confidence": 0.75,
                              "perspective": "according to revisionist historians" if "historians" in low else None})
    return {"entities": list(entities.values())[:80], "relations": relations[:120], "facts": facts[:80]}


class FakeLLMProvider(LLMProvider):
    name = "fake"

    def __init__(self, request_limit: int = 32_000) -> None:
        self.calls: list[dict[str, Any]] = []
        self._scripts: dict[str, list[Any]] = {}
        # Like Groq, reject any single request whose prompt + max_tokens exceeds this limit.
        self.request_limit = request_limit

    def script(self, task: str, response: dict | str | Callable[[list[ChatMessage]], Any]) -> None:
        """Queue a response for the next call of ``task`` (dict -> JSON)."""
        self._scripts.setdefault(task, []).append(response)

    def model_for(self, tier: Tier) -> str:
        return f"fake-{tier}"

    def context_tokens(self, tier: Tier) -> int:
        return self.request_limit

    async def complete(self, messages: list[ChatMessage], *, task: str, tier: Tier = "smart", json_mode: bool = False,
                       max_tokens: int | None = None, temperature: float | None = None) -> LLMResult:
        from app.core.errors import ProviderError
        from app.core.tokens import count_tokens

        requested = sum(count_tokens(m.content) for m in messages) + (max_tokens or 0)
        self.calls.append({"task": task, "tier": tier, "messages": messages, "requested_tokens": requested})
        if requested > self.request_limit:
            raise ProviderError(f"Request too large for '{task}': {requested} > {self.request_limit}",
                                retryable=False)
        queued = self._scripts.get(task)
        if queued:
            item = queued.pop(0)
            data = item(messages) if callable(item) else item
        else:
            handler = getattr(self, "_" + re.sub(r"[^a-z_]", "_", task), None)
            if handler is None and task.startswith("notes_section"):
                handler = self._notes_section
            if handler is None:
                raise RuntimeError(f"FakeLLMProvider has no handler for task '{task}'")
            data = handler(messages)
        text = data if isinstance(data, str) else json.dumps(data)
        return LLMResult(text=text, model=self.model_for(tier), usage=LLMUsage(len(str(messages)) // 4,
                                                                               len(text) // 4))

    # ------------------------------------------------------------------ handlers

    @staticmethod
    def _user(messages: list[ChatMessage]) -> str:
        return next((m.content for m in reversed(messages) if m.role == "user"), "")

    @staticmethod
    def _system(messages: list[ChatMessage]) -> str:
        return next((m.content for m in messages if m.role == "system"), "")

    def _extract(self, messages):
        return extract_rules(_tags("passage", self._user(messages)))

    def _summarize(self, messages):
        user = self._user(messages)
        prev = re.search(r"Previous summary:\n(.*?)\n\nNew messages:", user, re.S)
        lines = re.findall(r"^\[user\] (.{1,120})", user, re.M)
        base = "" if not prev or prev.group(1).strip() == "(none)" else prev.group(1).strip()
        summary = (base + " " if base else "") + "Teacher discussed: " + "; ".join(lines)
        return {"summary": summary[-5000:]}

    def _plan_turn(self, messages):
        user = self._user(messages)
        msg = user.split("Teacher message:\n", 1)[-1].strip()
        low = msg.lower()
        topic = re.search(r"Topic: (.*)", user)
        topic = topic.group(1).strip() if topic else ""
        known = re.search(r"Known concepts: (.*)", user)
        known_list = [k.strip() for k in known.group(1).split(",")] if known else []
        strong = re.search(r"local_retrieval: (\d+) strong", user)
        strong_hits = int(strong.group(1)) if strong else 0
        plan: dict[str, Any] = {"intent": "question", "needs_external_research": strong_hits < 3,
                                "search_queries": [], "focus_concepts": [], "navigation": {"action": "none"},
                                "feedback": [], "preferences": [], "mark_completed_concepts": [],
                                "resolved_questions": []}
        nav = None
        if "next phase" in low:
            nav = {"action": "next_phase"}
        elif "skip" in low and "phase" in low:
            nav = {"action": "skip_phase"}
        elif re.search(r"(finished|completed|done with) (this|the) phase", low):
            nav = {"action": "complete_phase"}
        elif m := re.search(r"(go back to|revisit|start|go to) phase (\d+)", low):
            nav = {"action": "goto_phase", "phase_ref": m.group(2)}
        if nav:
            plan["navigation"] = nav
            plan["intent"] = "navigation"
            plan["needs_external_research"] = False
        if re.search(r"\b(research|search|find sources|look up|latest)\b", low):
            plan["intent"] = "research_request"
            plan["needs_external_research"] = True
        if re.search(r"\b(hi|hello|thanks|thank you)\b", low) and len(low) < 30:
            plan["intent"] = "smalltalk"
            plan["needs_external_research"] = False
        focus = [k for k in known_list if k and k.lower() in low]
        caps = re.findall(r"\b([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)\b", msg)
        focus += [c for c in caps if c.lower() not in {"i", "what", "how", "why", "explain", "tell", "let",
                                                       "teach", "please", "can", "the", "phase"}]
        plan["focus_concepts"] = list(dict.fromkeys(focus))[:8]
        if m := re.search(r"i (?:understand|get) ([\w\s-]+)", low):
            plan["feedback"] = [{"concept": m.group(1).strip()[:100], "understanding": "understood"}]
            plan["intent"] = "feedback"
            plan["needs_external_research"] = False
        if m := re.search(r"confused about ([\w\s-]+)", low):
            plan["feedback"] = [{"concept": m.group(1).strip()[:100], "understanding": "confused"}]
        if m := re.search(r"(?:please|always) use ([\w\s-]+?)(?:\.|$)", low):
            plan["preferences"] = [{"key": "style", "value": m.group(1).strip()}]
        if m := re.search(r"i(?:'ve| have) mastered ([\w\s-]+)", low):
            plan["mark_completed_concepts"] = [m.group(1).strip()]
        if plan["needs_external_research"]:
            plan["search_queries"] = [f"{topic} {msg[:120]}".strip()]
        return plan

    def _answer(self, messages):
        system = self._system(messages)
        sources = _tags("source", system)
        parts, cited = [], []
        for attrs, text in sources[:3]:
            sents = _sentences(text)
            if sents:
                parts.append(f"{sents[0]} [{attrs['id']}]")
                cited.append(attrs["id"])
        user = self._user(messages)
        q = user.split("Teacher's new message:\n")[-1][:200]
        body = (f"**Answer to:** {q}\n\n" + ("\n\n".join(parts) if parts else
                                            "General background (not from researched sources): no researched "
                                            "passages cover this yet."))
        concepts = re.findall(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?)\b", q)
        return {"answer_markdown": body, "cited_sources": cited,
                "concepts_explained": list(dict.fromkeys(c for c in concepts if c not in {
                    "What", "How", "Why", "Explain", "Teach", "Let", "Tell", "Answer", "Phase"}))[:5],
                "open_questions": [] if parts else [q[:150]], "suggested_next": ["Continue to the next concept"],
                "evidence_limitations": None if parts else "No relevant researched passages were found."}

    def _roadmap(self, messages):
        user = self._user(messages)
        only = re.search(r"Regenerate ONLY the phase '([^']+)'", user)
        concepts = re.findall(r"^- (.+?) \((\w+); links=", user, re.M)
        topic = re.search(r"Topic: (.*)", user).group(1).strip()
        if not concepts:
            concepts = [(f"Introduction to {topic}", "Concept"), (f"Core ideas of {topic}", "Concept"),
                        (f"Applications of {topic}", "Concept")]
        events = sum(1 for _, cat in concepts if cat == "Event")
        org = "chronological" if events >= 2 else "prerequisite"
        names = [n for n, _ in concepts][:15]
        chunks = [names[i:i + 3] for i in range(0, len(names), 3)]
        titles = ["Foundations", "Core mechanisms", "Applications and connections", "Advanced topics",
                  "Synthesis"]
        phases = []
        for i, chunk in enumerate(chunks):
            title = f"{titles[i % len(titles)]}: {chunk[0]}"
            phases.append({"title": title, "description": f"Covers {', '.join(chunk)}.",
                           "objectives": [f"Explain {c}" for c in chunk], "estimated_minutes": 45,
                           "items": [{"concept": c, "description": f"Understand {c}.",
                                      "objectives": [f"Define {c}"], "examples": [f"Example of {c}"],
                                      "prerequisites": [chunk[j - 1]] if j else [],
                                      "estimated_minutes": 15} for j, c in enumerate(chunk)]})
        if only:
            phases = [{**phases[0], "title": only.group(1),
                       "items": [{**it, "description": f"Regenerated: {it['description']}"}
                                 for it in phases[0]["items"]]}]
        return {"organization": org, "summary": f"Roadmap for {topic}.", "phases": phases}

    def _notes_outline(self, messages):
        user = self._user(messages)
        names = re.findall(r"^- (.+)$", user, re.M)
        return {"sections": [{"title": f"Part {i // 3 + 1}: {names[i]}", "concepts": names[i:i + 3]}
                             for i in range(0, len(names), 3)] or [{"title": "Overview", "concepts": ["x"]}]}

    def _notes_section(self, messages):
        user = self._user(messages)
        title = re.search(r"Section \d+ of \d+: (.*)", user).group(1).strip()
        concepts = re.findall(r"^- ([^|\n]+?)(?: \||$)", user.split("Concepts in this section (in order):")[1]
                              .split("\n\nCoverage")[0], re.M)
        sources = _tags("source", user)
        body = [f"### Overview\n{title} introduces {', '.join(concepts)}."]
        for i, c in enumerate(concepts):
            src = sources[i % len(sources)] if sources else None
            sent = _sentences(src[1])[0] if src and _sentences(src[1]) else f"{c} is a key idea in this section."
            cite = f" [{src[0]['id']}]" if src else ""
            body.append(f"### {c}\n**Definition.** {sent}{cite}\n\n*Example:* A classroom example of {c}.")
        body.append("| Concept | Where it appears |\n|---|---|\n" + "\n".join(f"| {c} | {title} |" for c in concepts))
        body.append("A simple relation used for illustration: $a^2 + b^2 = c^2$.")
        return {"heading": title, "body_markdown": "\n\n".join(body),
                "key_takeaways": [f"{c} matters" for c in concepts[:3]],
                "misconceptions": [f"Students may confuse {concepts[0]} with a simpler idea"] if concepts else [],
                "teaching_strategies": ["Start from a concrete example"],
                "discussion_prompts": [f"Why is {concepts[0]} important?"] if concepts else [],
                "practice_exercises": [f"Explain {c} in your own words." for c in concepts[:2]],
                "cited_sources": [s[0]["id"] for s in sources[:2]], "concepts_covered": concepts}

    def _questions(self, messages):
        user = self._user(messages)
        slots = re.findall(r"^- slot_id=([^;]+); type=(\w+); marks=([\d.]+); difficulty=(\w+); concept=(.+)$",
                           user, re.M)
        rejected = set(re.findall(r"slot_id=([^;]+);[^\n]*\n  PREVIOUS ATTEMPT WAS REJECTED", user))
        sources = _tags("source", user)
        all_sents = [(a["id"], s) for a, t in sources for s in _sentences(t)]
        subject = (re.search(r"Subject: (.*)", user) or re.search(r"(.*)", "course")).group(1).strip()
        out = []
        for n, (sid, qtype, marks, diff, concept) in enumerate(slots):
            marks_f = float(marks)
            shift = 7 if sid in rejected else 0
            relevant = [x for x in all_sents if concept.lower() in x[1].lower()] or all_sents
            marker, sent = relevant[(n + shift) % len(relevant)] if relevant else ("S1", f"{concept} is studied.")
            base = {"slot_id": sid, "type": qtype, "difficulty": diff, "concepts": [concept],
                    "cited_sources": [marker], "explanation": f"The material states: {sent}"}
            if qtype == "mcq":
                k = (n + shift) % 4
                wrong = [f"{concept} has no connection to {subject} (option {n})",
                         f"{concept} was first described in the year {1000 + n * 7}",
                         f"{concept} only applies to marine biology ({n})"]
                choices = wrong[:k] + [sent[:160]] + wrong[k:]
                stem = (f"According to the researched sources, which statement about {concept} is accurate?"
                        if shift else f"Which statement about {concept} is supported by the course material?")
                q = {**base, "text": f"{stem} (item {sid})", "choices": choices, "correct_choice": k,
                     "answer": sent[:160]}
            elif qtype == "true_false":
                q = {**base, "text": f"True or false: {sent}", "answer": "True"}
            elif qtype == "fill_blank":
                pattern = re.compile(re.escape(concept), re.I)
                text = pattern.sub("_____", sent, count=1) if pattern.search(sent) else f"{sent} The concept " \
                                                                                        f"described is _____."
                q = {**base, "text": f"Fill in the blank: {text}", "answer": concept}
            elif qtype == "numerical":
                a, b = 3 + n + shift, 4 + 2 * n
                q = {**base, "text": f"In a {concept} exercise a quantity grows from {a} by a factor of {b}. "
                                     "What is the final value?", "numeric_answer": float(a * b), "units": "units",
                     "tolerance": 0.01, "answer_expression": f"{a} * {b}", "answer": f"{a * b} units"}
            elif qtype == "coding":
                q = {**base, "text": f"Write a Python function add(a, b) used in a {concept} example ({sid}).",
                     "answer": "Return the sum of the two inputs.", "code_language": "python",
                     "reference_solution": "def add(a, b):\n    return a + b",
                     "code_tests": "assert add(2, 3) == 5\nassert add(-1, 1) == 0"}
            else:
                prompt = {"short_answer": "Briefly explain", "long_answer": "Discuss in detail",
                          "application": "Apply the idea to a new situation:"}[qtype]
                q = {**base, "text": f"{prompt} the following statement about {concept}: \"{sent}\"",
                     "answer": f"A good answer explains that {sent}",
                     "rubric": [{"criterion": "Correct core idea", "marks": marks_f / 2},
                                {"criterion": "Clear explanation with example", "marks": marks_f / 2}]}
            if shift and qtype != "mcq":  # a repaired question is reworded, as a real model would
                q["text"] = f"Considering another angle of {concept}: {q['text']}"
            out.append(q)
        return {"questions": out}
