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
from app.llm.citations import cited_markers, rewrite_citations
from app.llm.structured import UNTRUSTED_POLICY, generate_structured, output_budget
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


@dataclass
class _Turn:
    """Mutable state accumulated while handling one chat turn."""

    message_id: str
    outcome: ResearchOutcome = field(default_factory=ResearchOutcome)
    graph_job: str | None = None
    preface: str = ""
    plan: TurnPlan | None = None
    nav: dict[str, Any] | None = None
    guided: dict[str, Any] = field(default_factory=dict)
    sufficient: bool = False
    research_performed: bool = False
    lesson_queries: list[str] = field(default_factory=list)
    extra_meta: dict[str, Any] = field(default_factory=dict)

    def merge_outcome(self, other: ResearchOutcome) -> None:
        for name in ("queries", "cached_queries", "new_source_ids", "reused_source_ids", "failures"):
            getattr(self.outcome, name).extend(getattr(other, name))


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
        from app.services.tutor.guide import quick_intent

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
        tutor = self.c.tutor
        quick = quick_intent(question)
        turn = _Turn(message_id=message_id)
        if progress:
            await progress("planning", 5)
        rm = await self.c.roadmap.current(ws_id)

        # 1) Guided kickoff: a beginner (or anyone saying start/next) without a roadmap gets the roadmap first.
        if rm is None and (workspace.familiarity_level == 0 or quick in ("start", "next")):
            rm, turn.outcome, turn.graph_job = await tutor.kickoff(workspace, user_id, session_id, progress)
            turn.preface = tutor.roadmap_markdown(workspace, rm)
            turn.guided = {"kickoff": True, "phase_count": len([p for p in rm.phases if p.included]),
                           "roadmap_version": rm.version}
            if quick in ("start", "next") or not question.strip():
                return await self._finish(turn, user_id, ws_id, session_id, turn.preface, progress=progress)

        # 2) Teaching pack on request.
        if quick == "pack":
            return await self._pack_reply(turn, workspace, user_id, session_id, progress)

        # 3) Plan: quick commands are understood deterministically; everything else goes to the planner.
        if quick in ("next", "start") and rm is not None:
            action = "next_phase" if quick == "next" or not state.get("current_phase_id") else "revisit_phase"
            plan = TurnPlan(intent="navigation", navigation=NavigationAction(action=action))
        else:
            plan = await self._plan(workspace, question, state, summary, ws_id)
            if plan.intent == "generate_request":
                return await self._pack_reply(turn, workspace, user_id, session_id, progress, plan=plan)
        turn.plan = plan

        # 4) Navigation (phase progression) and lesson mode.
        lesson = None
        if plan.navigation.action != "none":
            turn.nav = await self.c.roadmap.navigate(ws_id, session_id, plan.navigation.action,
                                                     plan.navigation.phase_ref)
            if turn.nav.get("ok"):
                turn.guided.update({k: turn.nav.get(k) for k in ("phase_index", "phase_count", "finished")})
                turn.guided["phase_title"] = turn.nav.get("current_phase")
                turn.guided["phase_key"] = turn.nav.get("current_phase_key")
                if turn.nav.get("finished"):
                    return await self._finish(turn, user_id, ws_id, session_id,
                                              tutor.finished_markdown(workspace, turn.nav["phase_count"]),
                                              progress=progress)
                if plan.navigation.action in ("start_phase", "next_phase", "goto_phase", "revisit_phase",
                                              "previous_phase", "skip_phase"):
                    rm = await self.c.roadmap.current(ws_id)
                    phase = next(p for p in rm.phases if p.key == turn.nav["current_phase_key"])
                    lesson = await tutor.prepare_lesson(workspace, user_id, session_id, phase,
                                                        turn.nav["phase_index"], turn.nav["phase_count"], progress)
                    turn.guided["lesson"] = {"weak_concepts": lesson.weak_concepts,
                                             "research_queries": lesson.research_queries,
                                             "new_sources": len(lesson.new_source_ids)}
            async with self.c.db.session() as s:
                state = session_state(await s.get(ResearchSession, session_id))

        # 5) Research decision for ordinary questions (lessons research per phase in prepare_lesson).
        local: list[RetrievedPassage] = []
        focus_entities: list[dict] = []
        if lesson is None:
            local = await self.c.retrieval.search(ws_id, question, top_k=self.settings.retrieval_top_k)
            strong = [p for p in local if p.score >= self.settings.retrieval_min_score]
            for name in plan.focus_concepts[:5]:
                focus_entities.extend(await self.c.graph.search_entities(ws_id, name, limit=2))
            focus_facts = await self.c.graph.facts_for_entities(ws_id, [e["id"] for e in focus_entities], limit=10)
            graph_supported = bool(plan.focus_concepts) and len(focus_facts) >= 2 and any(
                p.score >= self.settings.retrieval_min_score * 0.8 for p in local)
            turn.sufficient = len(strong) >= self.settings.sufficiency_min_hits or graph_supported
            explicit = plan.intent == "research_request"
            do_research = bool(plan.search_queries) and (
                explicit or (plan.needs_external_research and not turn.sufficient)
                or (not local and plan.intent in ("question", "explain")))
            if do_research:
                extra = await self.research(ws_id, plan.search_queries, progress)
                turn.merge_outcome(extra)
                turn.graph_job = await self.enqueue_graph_build(user_id, ws_id, extra.new_source_ids,
                                                                session_id) or turn.graph_job
                if extra.new_source_ids:
                    local = await self.c.retrieval.search(ws_id, question, top_k=self.settings.retrieval_top_k)
        else:
            turn.research_performed = bool(lesson.research_queries)
            turn.lesson_queries = lesson.research_queries

        # 6) Grounded answer (lesson or question).
        if progress:
            await progress("retrieving knowledge", 60)
        roadmap_ctx = await self.c.roadmap.phase_context(ws_id, state)
        if lesson is not None:
            passages = lesson.passages
        else:
            passages = [p for p in local if p.score >= self.settings.retrieval_min_score * 0.8] or local[:3]
        chunk_entities = await self.c.graph.entities_for_chunks(ws_id, [p.chunk_id for p in passages])
        entity_ids = list(dict.fromkeys([e["id"] for e in focus_entities] + [e["id"] for e in chunk_entities]))[:12]
        facts = await self.c.graph.facts_for_entities(ws_id, entity_ids, limit=30)
        markers = {f"S{i}": p for i, p in enumerate(passages, start=1)}
        mode_name, mode_text = FAMILIARITY[workspace.familiarity_level]
        system = self._answer_system(workspace, mode_text, roadmap_ctx)
        user_text = question if lesson is None else (
            f"{tutor.lesson_instructions(lesson)}\n\nTeacher's message: {question}")
        inputs = ContextInputs(
            system=system, user_message=user_text, summary=summary,
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
        answer_tokens = output_budget(self.c.llm, tier, 3500 if lesson is not None else 2500)
        built = build_context(inputs, make_budget(self.c.llm.context_tokens(tier), answer_tokens + 400,
                                                  self.settings.context_budget_ratio))
        if progress:
            await progress("writing lesson" if lesson is not None else "writing answer", 75)
        answer = await generate_structured(
            self.c.llm, task="answer", system=built.messages[0]["content"],
            user=self._flatten_history(built.messages[1:]), schema=AnswerOutput, tier=tier, max_tokens=answer_tokens)
        text, citations = self._ground(answer, {m: markers[m] for m in built.included_passages})
        if turn.preface:
            text = f"{turn.preface}\n\n---\n\n{text}"
        explained = answer.concepts_explained
        if lesson is not None:  # a delivered lesson explains the phase's concepts
            explained = list(dict.fromkeys([it.title for it in lesson.phase.items if it.included] + explained))
        turn.extra_meta = {
            "concepts_explained": explained, "suggested_next": answer.suggested_next,
            "evidence_limitations": answer.evidence_limitations,
            "context": {"tokens": built.used_tokens, "passages": len(built.included_passages),
                        "history_messages": built.included_history, "graph_facts": len(facts)}}
        return await self._finish(turn, user_id, ws_id, session_id, text, citations=citations,
                                  open_questions=answer.open_questions, explained=explained, progress=progress)

    async def _plan(self, workspace: Workspace, question: str, state: dict, summary: str, ws_id: str) -> TurnPlan:
        local = await self.c.retrieval.search(ws_id, question, top_k=self.settings.retrieval_top_k)
        strong = [p for p in local if p.score >= self.settings.retrieval_min_score]
        roadmap_ctx = await self.c.roadmap.phase_context(ws_id, state)
        known = (await self.c.graph.concept_outline(ws_id, limit=40))["nodes"]
        return await generate_structured(
            self.c.llm, task="plan_turn", system=PLAN_SYSTEM, tier="fast", schema=TurnPlan, max_tokens=800,
            user=(f"Topic: {workspace.topic}\nFamiliarity: {FAMILIARITY[workspace.familiarity_level][0]}\n"
                  f"Known concepts: {', '.join(k['name'] for k in known) or '(none yet)'}\n"
                  f"Current phase: {roadmap_ctx.get('current_phase_title') or '(none)'}\n"
                  f"local_retrieval: {len(strong)} strong hits (best score "
                  f"{max((p.score for p in local), default=0):.2f})\n"
                  f"Recent conversation summary: {summary[:1500] or '(none)'}\n\n"
                  f"Teacher message:\n{question}"))

    async def _pack_reply(self, turn: _Turn, workspace: Workspace, user_id: str, session_id: str, progress,
                          plan: TurnPlan | None = None) -> dict[str, Any]:
        turn.plan = plan or TurnPlan(intent="generate_request")
        pack_job = await self.c.tutor.enqueue_pack(workspace, user_id)
        turn.guided["pack_job_id"] = pack_job
        text = ((turn.preface + "\n\n---\n\n") if turn.preface else "") + (
            "Generating your **teaching pack**: teacher notes, student notes, a question paper and a separate "
            "answer key, all as PDFs, built from what you researched and covered. Progress is shown below; "
            "the downloads appear here and in *PDF exports* when it finishes.")
        return await self._finish(turn, user_id, workspace.id, session_id, text, progress=progress)

    async def _finish(self, turn: _Turn, user_id: str, ws_id: str, session_id: str, text: str, *,
                      citations: list[dict] | None = None, open_questions: list[str] | None = None,
                      explained: list[str] | None = None, progress=None) -> dict[str, Any]:
        plan = turn.plan or TurnPlan(intent="navigation")
        meta = {
            "reply_to": turn.message_id, "intent": plan.intent,
            "research": {
                "performed": turn.research_performed or bool(turn.outcome.queries),
                "sufficient_locally": turn.sufficient,
                "queries": turn.outcome.queries or turn.lesson_queries,
                "cached_queries": turn.outcome.cached_queries, "new_sources": len(turn.outcome.new_source_ids),
                "reused_sources": len(turn.outcome.reused_source_ids), "failures": turn.outcome.failures[:5],
                "graph_job_id": turn.graph_job},
            "navigation": turn.nav, "guided": turn.guided or None, **turn.extra_meta,
        }
        async with self.c.db.session() as s:
            sess = (await s.execute(select(ResearchSession).where(ResearchSession.id == session_id))).scalar_one()
            reply = await append_message(s, sess, "assistant", text, citations=citations or [], meta=meta)
            reply_id = reply.id
            new_state = session_state(sess)
            if plan.focus_concepts:
                new_state["current_concept"] = plan.focus_concepts[0]
                new_state["recent_concepts"] = list(dict.fromkeys(
                    plan.focus_concepts + new_state.get("recent_concepts", [])))[:15]
            unresolved = [q for q in new_state.get("unresolved_questions", []) if q not in plan.resolved_questions]
            new_state["unresolved_questions"] = list(dict.fromkeys(unresolved + (open_questions or [])))[-20:]
            sess.state = new_state
            for p in plan.preferences:
                stmt = pg_insert(Preference).values(user_id=user_id, scope=ws_id, key=p.key,
                                                    value={"text": p.value}, origin="explicit")
                await s.execute(stmt.on_conflict_do_update(index_elements=["user_id", "scope", "key", "origin"],
                                                           set_={"value": stmt.excluded.value}))
        await self.c.coverage.record_turn(
            ws_id, discussed=plan.focus_concepts, explained=explained or [],
            completed=plan.mark_completed_concepts, feedback=[f.model_dump() for f in plan.feedback],
            message_id=reply_id)
        if progress:
            await progress("updating memory", 92)
        async with self.c.db.session() as s:
            sess = (await s.execute(select(ResearchSession).where(ResearchSession.id == session_id))).scalar_one()
            await maybe_summarize(s, self.c.llm, sess, keep_recent=self.settings.context_recent_messages,
                                  trigger_tokens=self.settings.summary_trigger_tokens)
        return {"assistant_message_id": reply_id, "research_performed": meta["research"]["performed"],
                "new_sources": turn.outcome.new_source_ids, "graph_job_id": turn.graph_job,
                "guided": turn.guided or None}

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
        text = rewrite_citations(answer.answer_markdown, lambda m: f"[{m}]" if m in allowed else "")
        allowed_urls = {p.url for p in allowed.values() if p.url}
        text = _URL_RE.sub(lambda m: m.group(0) if m.group(0).rstrip(".,;") in allowed_urls
                           else "[link removed: not from a retrieved source]", text)
        used = list(dict.fromkeys(cited_markers(text) + [c for c in answer.cited_sources if c in allowed]))
        citations = [{"marker": m, "source_id": allowed[m].source_id, "chunk_id": allowed[m].chunk_id,
                      "title": allowed[m].title, "url": allowed[m].url,
                      "snippet": allowed[m].text[:280]} for m in used if m in allowed]
        return text, citations
