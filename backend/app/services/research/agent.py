"""Research agent: one chat turn = plan -> (maybe) research -> retrieve -> answer -> update state.

Runs inside the worker as a ``chat_turn`` job. Graph extraction for newly
ingested sources is enqueued as a follow-up ``graph_extract`` job so answers
are not blocked on (rate-limited) extraction calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.ids import normalize_key, sha256_text
from app.core.logging import get_logger
from app.db.models import Message, Preference, ResearchSession, SearchCache, Workspace
from app.jobs.queue import enqueue
from app.llm.structured import UNTRUSTED_POLICY, generate_structured
from app.providers.search.base import SearchHit
from app.services.conversation.context import ContextInputs, build_context, make_budget
from app.services.conversation.memory import append_message, maybe_summarize, session_state, unsummarized_messages
from app.services.research.retrieval import RetrievedPassage

if TYPE_CHECKING:
    from app.container import Container

log = get_logger(__name__)

FAMILIARITY = {
    0: ("beginner", "The teacher is new to this subject and must learn it before teaching it. Teach step by step "
                    "from first principles, define every new term, use analogies and concrete examples, and end "
                    "with one short check-for-understanding question. Stay within the current phase unless the "
                    "teacher asks to move on or explore something related."),
    1: ("familiar", "The teacher understands the basics and wants structured preparation. Be organized and "
                    "concise, emphasize what matters for teaching, typical student difficulties and good examples."),
    2: ("advanced", "The teacher is an expert. Be precise and technical, highlight nuances, edge cases, "
                    "common misconceptions and what to emphasize in teaching materials."),
}

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_MARKER_RE = re.compile(r"\[(S\d{1,3})\]")


class NavigationAction(BaseModel):
    action: Literal["none", "start_phase", "next_phase", "previous_phase", "goto_phase", "skip_phase",
                    "complete_phase", "revisit_phase"] = "none"
    phase_ref: str | None = Field(default=None, max_length=200)


class FeedbackItem(BaseModel):
    concept: str = Field(max_length=200)
    understanding: Literal["understood", "partially", "confused"]
    note: str = Field(default="", max_length=500)


class PreferenceItem(BaseModel):
    key: str = Field(max_length=60)
    value: str = Field(max_length=300)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", v.lower()).strip("_")[:60] or "preference"


class TurnPlan(BaseModel):
    intent: Literal["question", "explain", "research_request", "navigation", "feedback", "preference",
                    "generate_request", "smalltalk", "other"] = "question"
    needs_external_research: bool = False
    search_queries: list[str] = Field(default_factory=list, max_length=3)
    focus_concepts: list[str] = Field(default_factory=list, max_length=8)
    navigation: NavigationAction = Field(default_factory=NavigationAction)
    feedback: list[FeedbackItem] = Field(default_factory=list, max_length=5)
    preferences: list[PreferenceItem] = Field(default_factory=list, max_length=5)
    mark_completed_concepts: list[str] = Field(default_factory=list, max_length=10)
    resolved_questions: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("search_queries")
    @classmethod
    def _queries(cls, v: list[str]) -> list[str]:
        return [q.strip()[:200] for q in v if q and q.strip()][:3]


class AnswerOutput(BaseModel):
    answer_markdown: str = Field(min_length=1, max_length=20000)
    cited_sources: list[str] = Field(default_factory=list)
    concepts_explained: list[str] = Field(default_factory=list, max_length=12)
    open_questions: list[str] = Field(default_factory=list, max_length=6)
    suggested_next: list[str] = Field(default_factory=list, max_length=5)
    evidence_limitations: str | None = Field(default=None, max_length=1000)


PLAN_SYSTEM = f"""You are the planning step of a research assistant for teachers. Read the teacher's latest message
and the workspace context, then decide what to do. Rules:
- needs_external_research: true only if answering well requires information that is not already covered by the
  existing research (see 'local_retrieval' hits and known concepts), or the teacher explicitly asks to research,
  search, find sources or get recent information. Greetings, navigation, preferences and feedback never need it.
- search_queries: up to 3 focused web search queries (specific, self-contained, include the subject).
- focus_concepts: the main concepts the message is about.
- navigation: phase navigation requests such as "next phase", "go back to phase 2", "skip this phase",
  "I finished this phase".
- feedback: only explicit statements about the teacher's own understanding ("I get it", "I'm confused about X").
- preferences: only explicit, durable preferences ("use simpler language", "include more history").
- mark_completed_concepts: only concepts the teacher explicitly says they have completed or mastered.
{UNTRUSTED_POLICY}"""


@dataclass
class ResearchOutcome:
    queries: list[str] = field(default_factory=list)
    cached_queries: list[str] = field(default_factory=list)
    new_source_ids: list[str] = field(default_factory=list)
    reused_source_ids: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class ResearchAgent:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.settings = c.settings

    # ------------------------------------------------------------------ research

    async def cached_search(self, workspace_id: str, query: str) -> tuple[list[SearchHit], bool]:
        qhash = sha256_text(f"{self.c.search.name}|{normalize_key(query)}")
        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.search_cache_ttl_hours)
        async with self.c.db.session() as s:
            row = (await s.execute(select(SearchCache).where(
                SearchCache.workspace_id == workspace_id, SearchCache.query_hash == qhash))).scalar_one_or_none()
            if row is not None and row.created_at >= cutoff:
                return [SearchHit.model_validate(h) for h in row.results], True
        hits = await self.c.search.search(query, max_results=self.settings.search_max_results)
        async with self.c.db.session() as s:
            stmt = pg_insert(SearchCache).values(workspace_id=workspace_id, query=query, query_hash=qhash,
                                                 provider=self.c.search.name,
                                                 results=[h.model_dump() for h in hits])
            await s.execute(stmt.on_conflict_do_update(
                index_elements=["workspace_id", "query_hash"],
                set_={"results": stmt.excluded.results, "created_at": datetime.now(UTC)}))
        return hits, False

    async def research(self, workspace_id: str, queries: list[str], progress=None,
                       max_new_sources: int = 6) -> ResearchOutcome:
        out = ResearchOutcome()
        seen: set[str] = set()
        for i, q in enumerate(queries):
            if progress:
                await progress("searching", 15 + i * 10, q)
            try:
                hits, cached = await self.cached_search(workspace_id, q)
            except Exception as exc:  # one failed query should not sink the turn
                out.failures.append(f"search '{q}': {getattr(exc, 'message', str(exc))}")
                continue
            out.queries.append(q)
            if cached:
                out.cached_queries.append(q)
            for hit in hits:
                if hit.url in seen or len(out.new_source_ids) >= max_new_sources:
                    continue
                seen.add(hit.url)
                try:
                    res = await self.c.ingestion.ingest_search_hit(workspace_id, hit, q)
                except Exception as exc:
                    out.failures.append(f"ingest {hit.url}: {getattr(exc, 'message', str(exc))}")
                    continue
                if not res.source_id:
                    continue
                (out.new_source_ids if res.created else out.reused_source_ids).append(res.source_id)
        if progress and out.new_source_ids:
            await progress("indexing", 45, f"{len(out.new_source_ids)} new sources")
        for sid in out.new_source_ids:
            await self.c.ingestion.index_source(workspace_id, sid)
        return out

    async def enqueue_graph_build(self, user_id: str, workspace_id: str, source_ids: list[str],
                                  session_id: str | None) -> str | None:
        if not source_ids:
            return None
        key = f"graph_extract:{workspace_id}:{sha256_text(','.join(sorted(source_ids)))[:32]}"
        async with self.c.db.session() as s:
            job = await enqueue(s, kind="graph_extract", user_id=user_id, workspace_id=workspace_id,
                                payload={"source_ids": source_ids, "session_id": session_id},
                                idempotency_key=key, max_attempts=4)
            return job.id

    # ------------------------------------------------------------------ chat turn

    async def handle_turn(self, *, user_id: str, workspace: Workspace, session_id: str, message_id: str,
                          progress=None) -> dict[str, Any]:
        ws_id = workspace.id
        async with self.c.db.session() as s:
            sess = await s.get(ResearchSession, session_id)
            msg = await s.get(Message, message_id)
            if sess is None or msg is None or sess.workspace_id != ws_id or msg.session_id != session_id:
                raise ValueError("session/message not in workspace")
            existing_reply = (await s.execute(select(Message).where(
                Message.session_id == session_id, Message.role == "assistant",
                Message.meta["reply_to"].astext == message_id))).scalar_one_or_none()
            if existing_reply is not None:  # job retried after the reply was stored: idempotent
                return {"assistant_message_id": existing_reply.id, "reused": True}
            history = await unsummarized_messages(s, sess, before_seq=msg.seq)
            prefs = (await s.execute(select(Preference).where(
                Preference.user_id == user_id, Preference.scope.in_(["global", ws_id])))).scalars().all()
            state = session_state(sess)
            summary = sess.rolling_summary

        question = msg.content
        if progress:
            await progress("planning", 5)
        local = await self.c.retrieval.search(ws_id, question, top_k=self.settings.retrieval_top_k)
        strong = [p for p in local if p.score >= self.settings.retrieval_min_score]
        roadmap_ctx = await self.c.roadmap.phase_context(ws_id, state)
        known = (await self.c.graph.concept_outline(ws_id, limit=40))["nodes"]

        plan = await generate_structured(
            self.c.llm, task="plan_turn", system=PLAN_SYSTEM, tier="fast", schema=TurnPlan, max_tokens=800,
            user=(f"Topic: {workspace.topic}\nFamiliarity: {FAMILIARITY[workspace.familiarity_level][0]}\n"
                  f"Known concepts: {', '.join(k['name'] for k in known) or '(none yet)'}\n"
                  f"Current phase: {roadmap_ctx.get('current_phase_title') or '(none)'}\n"
                  f"local_retrieval: {len(strong)} strong hits (best score "
                  f"{max((p.score for p in local), default=0):.2f})\n"
                  f"Recent conversation summary: {summary[:1500] or '(none)'}\n\n"
                  f"Teacher message:\n{question}"))

        # sufficiency decision: the model suggests, deterministic rules decide. Existing knowledge counts as
        # sufficient when enough strong passages match, or when the focus concepts are already in the graph
        # with supporting claims and at least one relevant passage exists.
        explicit = plan.intent == "research_request"
        focus_entities: list[dict] = []
        for name in plan.focus_concepts[:5]:
            focus_entities.extend(await self.c.graph.search_entities(ws_id, name, limit=2))
        focus_facts = await self.c.graph.facts_for_entities(ws_id, [e["id"] for e in focus_entities], limit=10)
        graph_supported = bool(plan.focus_concepts) and len(focus_facts) >= 2 and any(
            p.score >= self.settings.retrieval_min_score * 0.8 for p in local)
        sufficient = len(strong) >= self.settings.sufficiency_min_hits or graph_supported
        do_research = bool(plan.search_queries) and (explicit or (plan.needs_external_research and not sufficient)
                                                     or (not local and plan.intent in ("question", "explain")))
        if do_research and not plan.search_queries:
            plan.search_queries = [f"{workspace.topic}: {question[:150]}"]
        outcome = ResearchOutcome()
        graph_job = None
        if do_research:
            outcome = await self.research(ws_id, plan.search_queries, progress)
            graph_job = await self.enqueue_graph_build(user_id, ws_id, outcome.new_source_ids, session_id)
            if outcome.new_source_ids:
                local = await self.c.retrieval.search(ws_id, question, top_k=self.settings.retrieval_top_k)

        nav_result = None
        if plan.navigation.action != "none":
            nav_result = await self.c.roadmap.navigate(ws_id, session_id, plan.navigation.action,
                                                       plan.navigation.phase_ref)
            async with self.c.db.session() as s:
                state = session_state(await s.get(ResearchSession, session_id))
            roadmap_ctx = await self.c.roadmap.phase_context(ws_id, state)

        if progress:
            await progress("retrieving knowledge", 60)
        passages = [p for p in local if p.score >= self.settings.retrieval_min_score * 0.8] or local[:3]
        entity_rows = focus_entities
        chunk_entities = await self.c.graph.entities_for_chunks(ws_id, [p.chunk_id for p in passages])
        entity_ids = list(dict.fromkeys([e["id"] for e in entity_rows] + [e["id"] for e in chunk_entities]))[:12]
        facts = await self.c.graph.facts_for_entities(ws_id, entity_ids, limit=30)

        markers = {f"S{i}": p for i, p in enumerate(passages, start=1)}
        mode_name, mode_text = FAMILIARITY[workspace.familiarity_level]
        system = self._answer_system(workspace, mode_text, roadmap_ctx)
        inputs = ContextInputs(
            system=system, user_message=question, summary=summary,
            session_state={k: v for k, v in {**state, "mode": mode_name,
                                             "current_phase": roadmap_ctx.get("current_phase_title"),
                                             "current_phase_objectives": roadmap_ctx.get("objectives"),
                                             "phase_concepts": roadmap_ctx.get("concepts")}.items() if v},
            preferences=[{"key": p.key, "value": p.value, "origin": p.origin} for p in prefs],
            graph_facts=facts,
            passages=[{"marker": m, "title": p.title, "url": p.url, "text": p.text} for m, p in markers.items()],
            recent_messages=[{"role": m.role, "content": m.content} for m in history if m.role in ("user",
                                                                                                    "assistant")],
        )
        tier = "smart"
        built = build_context(inputs, make_budget(self.c.llm.context_tokens(tier), 2500,
                                                  self.settings.context_budget_ratio))
        if progress:
            await progress("writing answer", 75)
        answer = await generate_structured(
            self.c.llm, task="answer", system=built.messages[0]["content"],
            user=self._flatten_history(built.messages[1:]), schema=AnswerOutput, tier=tier, max_tokens=2500)

        text, citations = self._ground(answer, {m: markers[m] for m in built.included_passages})
        meta = {
            "reply_to": message_id, "intent": plan.intent, "research": {
                "performed": do_research, "sufficient_locally": sufficient, "queries": outcome.queries, "cached_queries": outcome.cached_queries,
                "new_sources": len(outcome.new_source_ids), "reused_sources": len(outcome.reused_source_ids),
                "failures": outcome.failures[:5], "graph_job_id": graph_job},
            "concepts_explained": answer.concepts_explained, "suggested_next": answer.suggested_next,
            "evidence_limitations": answer.evidence_limitations, "navigation": nav_result,
            "context": {"tokens": built.used_tokens, "passages": len(built.included_passages),
                        "history_messages": built.included_history, "graph_facts": len(facts)},
        }
        async with self.c.db.session() as s:
            sess = (await s.execute(select(ResearchSession).where(ResearchSession.id == session_id))).scalar_one()
            reply = await append_message(s, sess, "assistant", text, citations=citations, meta=meta)
            reply_id = reply.id
            new_state = session_state(sess)
            if plan.focus_concepts:
                new_state["current_concept"] = plan.focus_concepts[0]
                new_state["recent_concepts"] = list(dict.fromkeys(
                    plan.focus_concepts + new_state.get("recent_concepts", [])))[:15]
            unresolved = [q for q in new_state.get("unresolved_questions", [])
                          if q not in plan.resolved_questions]
            new_state["unresolved_questions"] = list(dict.fromkeys(unresolved + answer.open_questions))[-20:]
            sess.state = new_state
            for p in plan.preferences:
                stmt = pg_insert(Preference).values(user_id=user_id, scope=ws_id, key=p.key,
                                                    value={"text": p.value}, origin="explicit")
                await s.execute(stmt.on_conflict_do_update(index_elements=["user_id", "scope", "key", "origin"],
                                                           set_={"value": stmt.excluded.value}))
        await self.c.coverage.record_turn(
            ws_id, discussed=plan.focus_concepts, explained=answer.concepts_explained,
            completed=plan.mark_completed_concepts,
            feedback=[f.model_dump() for f in plan.feedback], message_id=reply_id)
        if progress:
            await progress("updating memory", 92)
        async with self.c.db.session() as s:
            sess = (await s.execute(select(ResearchSession).where(ResearchSession.id == session_id))).scalar_one()
            await maybe_summarize(s, self.c.llm, sess, keep_recent=self.settings.context_recent_messages,
                                  trigger_tokens=self.settings.summary_trigger_tokens)
        return {"assistant_message_id": reply_id, "research_performed": do_research,
                "new_sources": outcome.new_source_ids, "graph_job_id": graph_job}

    @staticmethod
    def _flatten_history(messages: list[dict]) -> str:
        if len(messages) == 1:
            return messages[0]["content"]
        parts = [f"[{m['role']}]: {m['content']}" for m in messages[:-1]]
        return "Recent conversation:\n" + "\n\n".join(parts) + "\n\nTeacher's new message:\n" + messages[-1]["content"]

    def _answer_system(self, ws: Workspace, mode_text: str, roadmap_ctx: dict) -> str:
        lang = ws.language or "en"
        return (
            f"You are a research and teaching-preparation assistant for a teacher.\n"
            f"Topic: {ws.topic}. Scope: {ws.description or 'not specified'}.\n"
            f"Their students: {ws.audience or 'unspecified'} ({ws.academic_level or 'level unspecified'}).\n"
            f"Teaching goals: {'; '.join(ws.teaching_goals or []) or 'not specified'}.\n"
            f"Mode: {mode_text}\nRespond in language: {lang}.\n\n"
            "Grounding rules:\n"
            "- Base factual statements on the retrieved source passages and knowledge-graph facts when they cover "
            "the question, and cite passages inline with their markers like [S1] immediately after the claim.\n"
            "- Only use markers of passages that were provided. Never write URLs, paper titles or quotations that "
            "are not in the passages.\n"
            "- If you add general background knowledge not found in the passages, label it explicitly as "
            "'General background (not from researched sources)'.\n"
            "- If the evidence is insufficient or sources disagree, say so in evidence_limitations and present the "
            "differing perspectives with attribution instead of picking one silently.\n"
            "- Use Markdown; use $...$ for inline math and $$...$$ for display math.\n"
            f"{UNTRUSTED_POLICY}"
        )

    @staticmethod
    def _ground(answer: AnswerOutput, allowed: dict[str, RetrievedPassage]) -> tuple[str, list[dict]]:
        """Keep only citation markers for passages actually provided; strip URLs not from those sources."""
        text = answer.answer_markdown
        text = _MARKER_RE.sub(lambda m: m.group(0) if m.group(1) in allowed else "", text)
        allowed_urls = {p.url for p in allowed.values() if p.url}
        text = _URL_RE.sub(lambda m: m.group(0) if m.group(0).rstrip(".,;") in allowed_urls
                           else "[link removed: not from a retrieved source]", text)
        used = list(dict.fromkeys(_MARKER_RE.findall(text) + [c for c in answer.cited_sources if c in allowed]))
        citations = [{"marker": m, "source_id": allowed[m].source_id, "chunk_id": allowed[m].chunk_id,
                      "title": allowed[m].title, "url": allowed[m].url,
                      "snippet": allowed[m].text[:280]} for m in used if m in allowed]
        return text, citations
