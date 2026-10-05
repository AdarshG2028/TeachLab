# Architecture

## Overview

```
 Next.js UI ──HTTP/SSE──▶ FastAPI (thin: authz, CRUD, enqueue, SSE)
                               │  enqueue (jobs table + pg_notify)
                               ▼
                         Worker process(es)  ◀── claims jobs with FOR UPDATE SKIP LOCKED, lease + heartbeat
                               │
        ┌──────────────┬───────┼──────────────┬────────────────┐
        ▼              ▼       ▼              ▼                ▼
    Groq LLM      Exa/Tavily   BGE (local)    Qdrant          PostgreSQL
  (OpenAI API)    web search   embeddings     vectors         source of truth
                                              (per-workspace  (+ PDFs/uploads in
                                               filter)         file storage)
```

* **The API never loads the embedding model, and never calls the LLM or search.** Every slow or expensive operation runs as a durable job in the worker: chat turns, lessons, research, roadmaps, notes, tests, teaching packs and PDF export. The API stays at about 120 MB RSS. The worker loads BGE once per process (about 600 to 740 MB RSS, measured).
* **PostgreSQL is the source of truth.** It holds users, workspaces, sessions, messages, preferences, sources with their normalized text and chunks, roadmap versions, coverage, notes, assessments, questions, artifacts and jobs. Qdrant is a derived index: `POST /vectors/reindex` rebuilds it from the stored chunks.
* **No knowledge graph.** An earlier version extracted entities and claims into Neo4j. It was removed: it consumed most of the free-tier LLM budget, and notes, tests and lessons are built from source passages anyway. Provenance is kept through citations, since every answer, note and question links to the real source passages it used.

## Module boundaries

| Concern | Module | Notes |
|---|---|---|
| Provider interfaces | `app/providers/*/base.py` | `LLMProvider`, `SearchProvider`, `EmbeddingProvider`, `VectorStore`, `PDFProvider`, `StorageProvider`, `AuthProvider` |
| Wiring / DI | `app/container.py` | Lazy providers; tests swap them in (`container._llm = FakeLLMProvider()`) |
| Research orchestration | `services/research/agent.py` | quick commands → plan → sufficiency decision → (cached) search → ingest → index → retrieve → grounded answer/lesson → state update |
| Guided learning | `services/tutor/guide.py` | roadmap kickoff, per-phase lessons with just-in-time research, teaching pack |
| Ingestion | `services/research/ingestion.py`, `fetcher.py`, `documents.py`, `chunker.py` | URL/content dedup, SSRF-safe fetch, HTML (trafilatura) / PDF (pypdf), deterministic chunking |
| Retrieval | `services/research/retrieval.py` | vector search hydrated from PostgreSQL; `relevant_for_concept` (similarity + key-term check) |
| Concepts | `core/concepts.py` | concept-name normalization and keys, key-term matching |
| Conversation memory | `services/conversation/` | Messages, rolling summary, session state, token-budgeted context |
| Roadmap / coverage / gaps | `services/roadmap/` | Versioned plan from research passages; coverage events; gap report |
| Teaching content | `services/content/notes.py` | Section-wise generation from concept passages + coverage |
| Assessment | `services/assessment/` | Deterministic blueprint → generation → validation → repair → flags; answer key |
| Export | `services/export/service.py`, `providers/pdf/reportlab_provider.py` | Provider-neutral `DocumentSpec` → PDF |
| Jobs | `jobs/queue.py`, `worker/` | Durable queue, priorities, retries with backoff, lease recovery, idempotency keys |

## When does a source "cover" a concept?

Coverage, lesson research, notes and tests all ask the same question, answered by
`RetrievalService.relevant_for_concept`. A stored passage counts only if both of these hold:

1. its embedding similarity to the concept name is above `RETRIEVAL_MIN_SCORE`;
2. it mentions most (≥ 60%) of the concept's key terms, after stop-word removal and light stemming.

The second check matters. Measured with BGE-small, unrelated chunks from the same domain score 0.6–0.7, and
adding the topic to the query pushed even off-topic concepts above 0.85. Similarity alone marked almost
everything as covered.

## Conversation and context

* Full history is stored in PostgreSQL: `messages`, with a per-session monotonic `seq`.
* For each turn the context is assembled within a token budget:
  * the system prompt, which covers familiarity mode, audience, grounding rules and the untrusted-content policy;
  * the rolling summary;
  * structured session state (current phase and objectives, current concept, unresolved questions);
  * explicit preferences;
  * retrieved passages (`[S#]` markers, wrapped as `<untrusted_source>`);
  * then as many unsummarized messages as fit, newest first.

  Token counts are approximated with cl100k under a configurable safety ratio. Every request also fits
  `LLM_TOKENS_PER_MINUTE`, because Groq rejects any single request above the plan's TPM.
* Messages older than the recent window are folded into the rolling summary once they exceed `SUMMARY_TRIGGER_TOKENS`. The original messages are kept, and no message is ever invisible to the model: each is either summarized or included verbatim.
* Self-reported understanding ("I'm confused about X") is stored separately from explicit completion. Completion is never inferred.

## Research agent decision

The model proposes a `TurnPlan`; deterministic rules decide whether to search. External research runs only when one of these holds:

* the teacher explicitly asks for it;
* the model says research is needed and local knowledge isn't sufficient — meaning fewer than `SUFFICIENCY_MIN_HITS` strong vector hits, and no focus concept already has at least 2 relevant passages;
* nothing has been researched yet.

Searches go through a per-workspace cache keyed on the normalized query. Sources are deduplicated by canonical URL (lower-cased host, no `www.` or fragments, tracking parameters stripped, query parameters sorted) and by content hash.

**Grounding.**
* The answer may cite only the passage markers it was given. All citation styles are parsed: `[S1]`, `[S1, S2]` and the full-width `【S1】` that gpt-oss models produce. Unknown markers are stripped.
* URLs that don't belong to a retrieved source are replaced with "[link removed: not from a retrieved source]".
* General knowledge must be labelled "General background (not from researched sources)".
* Retrieved web content is wrapped in `<untrusted_*>` tags under an explicit policy, with the closing-tag escape neutralized. Model output is only ever parsed as schema-validated JSON and never executed.

## Guided learning flow (`services/tutor/guide.py`)

The default experience for a beginner (familiarity level 0), also available at other levels:

1. **Roadmap first.** The first message (or **start**) triggers a quick, broad search: overview, key concepts, prerequisites. A roadmap is planned from the retrieved passages and shown in the chat.
2. **One lesson per phase.** **next** completes the current phase and teaches the next one as a structured lesson: objectives; one section per concept with an explanation, example and citations; common misconceptions; and 2-3 check-your-understanding questions without answers.
   - Research happens just in time: only concepts no stored passage covers (see above) trigger focused searches.
   - Moving on marks the phase completed, but never marks concepts as mastered.
3. **Teaching pack.** After the last phase, or whenever asked (**generate teaching pack**, `POST /teaching-pack`), one `generate_pack` job produces teacher notes, student notes, a strict-coverage test, the answer key and all PDFs. If the test cannot be built, the notes PDFs are still delivered, with a warning.

The commands **start**, **next** and **generate teaching pack** are recognised deterministically, so they cost no planner LLM call. Anything else goes through the normal research/answer pipeline.

**Fairness under free-tier limits.**
- Interactive jobs (chat turns, lessons) are claimed before background jobs (vector reindexing), and one worker slot is kept free for them.
- Low-priority LLM calls (conversation summaries) leave about 35% of the per-minute token budget untouched.

## Roadmap, coverage, gaps

* **Roadmap.** A roadmap is a versioned teaching plan. The LLM plans it from research passages and from concepts already covered, choosing an organization: prerequisite, chronological, thematic, causal or mixed.
  * Concept items carry stable concept keys derived from normalized names, shared with coverage and tests.
  * Teacher edits create a new version, flagging `teacher_edited` on whatever changed, with optimistic concurrency via `expected_version`.
  * Regeneration merges edits forward: edited or excluded phases and items survive, and orphaned edited items move to a "Teacher additions" phase.
  * Phase navigation covers start, next, previous, go to, skip, complete and revisit. It updates phase status and the session's current phase.
* **Coverage.** Each concept has independent timestamps for planned, researched, explained, discussed and completed.
  * "Researched" means stored sources really discuss it; it is refreshed when roadmaps, notes and gap reports are built.
  * Completed is set only explicitly by the teacher.
* **Gaps.** The gap report distinguishes:
  * `required` gaps: prerequisites the plan declares but no phase covers, and planned concepts no source discusses;
  * `recommended` gaps: single-source concepts, missing examples or notes, and unresolved questions;
  * `info`: planned but not yet explained or discussed.

  It reports factual counts, never mastery percentages.

## Assessment pipeline

1. **Scope.**
   * *Strict:* concepts covered at least to "researched", or concepts in the selected notes, and only those with relevant passages. Requested topics that weren't covered are excluded and reported. If nothing is recorded yet, the roadmap's concepts are checked against the stored sources first.
   * *Expanded:* covered concepts plus other researched concepts from the same or neighbouring roadmap phases. Questions may apply them to new scenarios but must not introduce unresearched topics; mentions of out-of-scope concepts are flagged.
2. **Deterministic blueprint.** Sections and slots, each with type, marks, difficulty and target concept, built by largest-remainder allocation of the difficulty and topic distributions. Inconsistent totals are rejected with a 422 before any generation runs.
3. **Generation** in batches (sized to the per-request token budget), using the passages that discuss each slot's concept.
4. **Validation.**
   * Structure: types and choices, MCQ answer-key validity, distinct choices, true/false values, blanks.
   * Numeric answers are verified with a safe AST evaluator: no eval, bounded exponents.
   * Rubrics are rescaled so they sum to the question's marks.
   * Duplicates are caught both exactly and semantically (BGE cosine ≥ 0.92).
   * Strict grounding requires citations plus passage similarity.
   * Difficulty mismatches are flagged.
5. **Repair.** Up to 2 rounds regenerate only the failing slots, passing the rejection reasons back to the model. Whatever still fails is saved and flagged for review rather than dropped.
6. **Teacher edits.** Edit, remove, regenerate (the replacement must differ from the original), or add questions by hand. The validation report is recomputed after every change.
7. **Answer-key separation is structural.** The student paper is built from `StudentQuestionView`, which has no answer, explanation or rubric fields. Answers appear on the paper only when `include_answers_in_paper` is explicitly set.

## Jobs and reliability

* **Queue.**
  * `jobs` rows hold status, attempts and max_attempts, `run_after`, a lease, progress steps, the result and the error.
  * A unique `idempotency_key` means repeated enqueues return the same job.
  * `pg_notify` wakes the worker immediately, with polling as a fallback.
* **Failure handling.**
  * Retryable `ProviderError`s (rate limits, outages, invalid model JSON) back off and honor `retry-after`. Non-retryable errors fail immediately with a recorded message.
  * A crashed worker's job is reclaimed once its lease expires, and fails after `max_attempts`.
  * Every handler re-checks that the job user owns the workspace.
* **Idempotency** holds at each layer:
  * sources: `(workspace, url_hash)` plus a content hash;
  * chunks: stable IDs, which double as Qdrant point IDs;
  * artifacts: `(workspace, content fingerprint)`;
  * chat turns: an existing reply is detected and reused.
* **Partial failure.** A failed research query is recorded and the turn continues. A failed source ingest is skipped and reported. Indexing is stamped per source and can be replayed.

## Security

* Workspace authorization runs on every route. Another user's workspace returns 404 rather than 403, to avoid enumeration. Vector queries are always workspace-scoped, and chunk hydration re-checks ownership.
* **SSRF.** URL fetching has these protections:
  * only http(s) on standard ports, with no credentials in the URL;
  * DNS is resolved once, and every resolved IP must be public (no loopback, private, link-local, metadata or reserved addresses);
  * the connection is pinned to the validated IP, with the Host header and SNI preserved;
  * every redirect hop is re-validated;
  * content types are allow-listed and the response size is capped.
* **Uploads.** Extension allow-list, magic-byte check for PDFs, binary rejected for text types, size limit. Storage keys are validated against path traversal.
* **Secrets.** Kept server-side in `.env`, which is git-ignored. The production settings guard rejects local auth, the fake LLM and fixture search.
* **Code execution.** Generated code is off by default. When enabled, it runs as an isolated subprocess with a timeout; this is not a sandbox, so production must use an isolated container.

## Observability

* `structlog` key-value logs (JSON with `LOG_JSON=true`) are bound with `request_id`, `job_id`, `job_kind` and `workspace_id`.
* LLM calls log token usage.
* OpenTelemetry FastAPI tracing is available behind `OTEL_ENABLED` (`uv sync --extra otel`), exporting via OTLP or to the console.
