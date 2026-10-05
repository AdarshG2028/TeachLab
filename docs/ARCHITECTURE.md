# Architecture

## Overview

```
 Next.js UI ──HTTP/SSE──▶ FastAPI (thin: authz, CRUD, enqueue, SSE)
                               │  enqueue (jobs table + pg_notify)
                               ▼
                         Worker process(es)  ◀── claims jobs with FOR UPDATE SKIP LOCKED, lease + heartbeat
                               │
      ┌───────────────┬────────┼──────────────┬───────────────┬──────────────┐
      ▼               ▼        ▼              ▼               ▼              ▼
   Groq LLM    Exa/Tavily   BGE (local)    Qdrant         Neo4j          PostgreSQL
 (OpenAI API)   search/extract embeddings  vectors        knowledge     source of truth
                                           (per-workspace graph          (+ artifacts in
                                            filter)       (claims/evidence) file storage)
```

* **The API never loads the embedding model, and never calls the LLM or search.** Every slow or expensive operation runs as a durable job in the worker: chat turns, research, graph extraction, roadmap, notes, assessments and PDF export. As a result the API stays at about 120 MB RSS. The worker loads BGE once per process (about 600 to 740 MB RSS, measured).
* **PostgreSQL is the source of truth.** It holds users, workspaces, sessions, messages, preferences, sources (normalized text), chunks, raw validated LLM extractions, ontology proposals, roadmap versions, coverage, notes, assessments, questions, artifacts and jobs. Neo4j and Qdrant are derived stores, and both can be rebuilt from it:
  * `POST /graph/rebuild` replays the stored extractions and makes no LLM calls.
  * `POST /vectors/reindex` re-embeds the chunks.

## Module boundaries

| Concern | Module | Notes |
|---|---|---|
| Provider interfaces | `app/providers/*/base.py` | `LLMProvider`, `SearchProvider`, `EmbeddingProvider`, `VectorStore`, `PDFProvider`, `StorageProvider`, `AuthProvider` |
| Wiring / DI | `app/container.py` | Lazy providers; tests swap them in (`container._llm = FakeLLMProvider()`) |
| Research orchestration | `services/research/agent.py` | plan → sufficiency decision → (cached) search → ingest → index → retrieve → grounded answer → state update |
| Ingestion | `services/research/ingestion.py`, `fetcher.py`, `documents.py`, `chunker.py` | URL/content dedup, SSRF-safe fetch, HTML (trafilatura) / PDF (pypdf), deterministic chunking |
| Knowledge extraction | `services/knowledge/extractor.py` | LLM proposes entities / relations / facts with passage ids and verbatim quotes |
| Ontology resolution | `services/knowledge/ontology.py`, `relations.py` | Pure, deterministic; decides what is accepted |
| Graph persistence | `providers/graph/neo4j_store.py`, `services/knowledge/service.py` | Parameterized Cypher only, per-workspace advisory lock |
| Conversation memory | `services/conversation/` | Messages, rolling summary, session state, token-budgeted context |
| Roadmap / coverage / gaps | `services/roadmap/` | Versioned plan derived from the graph; coverage events; gap report |
| Teaching content | `services/content/notes.py` | Section-wise generation from graph facts + evidence passages + coverage |
| Assessment | `services/assessment/` | Deterministic blueprint → generation → validation → repair → flags; answer key |
| Export | `services/export/service.py`, `providers/pdf/reportlab_provider.py` | Provider-neutral `DocumentSpec` → PDF |
| Jobs | `jobs/queue.py`, `worker/` | Durable queue, retries with backoff, lease recovery, idempotency keys |

## Knowledge graph model (Neo4j)

**Nodes.** Every node has `id` (unique constraint) and `workspace_id` (index).

* `Entity`, plus one category label drawn from an allowlist: Concept, Person, Event, Place, Organization, Technology, Method or Other.
* `Source`, `Claim`, `Evidence`, `ResearchSession`, `TeachingMaterial`.

**Relationships.**
* Semantic relations use a single relationship type: `(:Entity)-[:REL {type, family, relation_status, claim_status, evidence_count}]->(:Entity)`. The relation type lives in a property, so the ontology can grow dynamically without dynamic Cypher. Only the category label is interpolated into a query, and only after an allowlist check. LLM-generated Cypher is never executed.
* Provenance edges:
  * `(:Claim)-[:ABOUT {role}]->(:Entity)`
  * `(:Claim)-[:SUPPORTED_BY]->(:Evidence)-[:FROM_SOURCE]->(:Source)`
  * `(:Claim)-[:CONTRADICTS]->(:Claim)`
  * `(:Claim)-[:DISCOVERED_IN]->(:ResearchSession)`
  * `(:TeachingMaterial)-[:COVERS]->(:Entity)`

**Claims.** Every relation and fact is stored as a `Claim`, never as an unquestioned edge. A claim records:
* its text, subject and object, the canonical predicate, and every raw predicate seen;
* its family, negation flag, time context and perspectives;
* the session IDs, a model confidence (a heuristic, labelled as such in the UI), and its status;
* one `Evidence` node per (claim, chunk), each with the verified verbatim quote, source, extraction ID and timestamp.

A claim backed by several sources therefore keeps separate, traceable evidence for each one.

**Claim status** is one of `unverified`, `supported`, `contested`, `rejected` or `teacher_verified`.
* `supported`: at least one evidence quote was verified verbatim against the passage.
* `contested`: the claim has a `CONTRADICTS` edge.
* `rejected` and `teacher_verified` are set by the teacher and never overwritten by later research.

**Isolation.** Every ID embeds its workspace (`uuid5(workspace, normalized key)`), and every query filters on `workspace_id`. Two teachers researching the same subject get disjoint subgraphs, and nothing is shared across workspaces.

## Hybrid dynamic ontology

1. **Extraction (LLM, fast model).** The LLM extracts candidate entities, free-text predicates ("is represented using", "was influenced by"), facts, a passage ID, a verbatim quote, negation, perspective, time context and a confidence value. The output is validated against a Pydantic schema, with a repair loop if it doesn't conform.
2. **Resolution (deterministic, `OntologyResolver`).** The resolver decides what is accepted:
   * **Entities.**
     * Names are normalized: NFKC, case-folded, singularized, with articles stripped and aliases split out of "X (Y)".
     * Exact key or alias matches are merged automatically.
     * Fuzzy and acronym matches become `entity_merge` proposals for review, never silent merges.
     * When categories are incompatible (a Person vs. a Place, say), the entity is kept as a separate homonym and a `homonym` proposal is created.
   * **Predicates.**
     * Predicates resolve against a small canonical registry of about 30 types in 11 families (taxonomic, compositional, prerequisite, causal, temporal, spatial, functional, comparative, attribution, teaching, association).
     * Matching uses stemmed synonym keys, inverse phrases (e.g. "consists of" becomes PART_OF with the direction reversed) and passive voice ("X by").
     * Exact matching is tried first, then fuzzy matching.
     * Symmetric relations are canonically ordered.
   * **Validation.**
     * Direction and type are checked against entity-category constraints. A swapped direction is auto-corrected when that resolves the violation; otherwise a `type_mismatch` proposal is created.
     * Self-loops and references to unknown passages are rejected.
     * Quotes that don't appear in the passage are dropped; quotations are never invented.
   * **New relation types.** An unknown predicate becomes a sanitized proposed type (`^[A-Z][A-Z0-9_]{1,47}$`) and a `new_relation_type` proposal.
     * Accepting it adds it to that workspace's registry.
     * Rejecting it remaps its edges to a canonical type, and the phrase is remembered as a synonym.
   * **Contradictions.** An assertion and an explicit denial of the same relation, or a single-valued relation (e.g. LOCATED_IN) with different objects, produce `CONTRADICTS` edges and a `contradiction` proposal. Both claims are kept; nothing is overwritten.
   * **Provenance.** Both the original and the normalized relation are recorded, together with the extraction ID.
3. **Persistence.** The raw extraction is stored in PostgreSQL first. The graph is then written idempotently with MERGE on stable IDs, under a PostgreSQL advisory lock per workspace. Re-running an extraction batch doesn't call the LLM again, because extractions are keyed by `(workspace, batch_key)`.

## Conversation and context

* Full history is stored in PostgreSQL: `messages`, with a per-session monotonic `seq`.
* For each turn the context is assembled within a token budget:
  * the system prompt, which covers familiarity mode, audience, grounding rules and the untrusted-content policy;
  * the rolling summary;
  * structured session state (current phase and objectives, current concept, unresolved questions);
  * explicit preferences;
  * relevant graph facts (with status and perspectives);
  * retrieved passages (`[S#]` markers, wrapped as `<untrusted_source>`);
  * then as many unsummarized messages as fit, newest first.

  Token counts are approximated with cl100k under a configurable safety ratio.
* Messages older than the recent window are folded into the rolling summary once they exceed `SUMMARY_TRIGGER_TOKENS`. The original messages are kept, and no message is ever invisible to the model: each is either summarized or included verbatim.
* Self-reported understanding ("I'm confused about X") is stored separately from explicit completion. Completion is never inferred.

## Research agent decision

The model proposes a `TurnPlan`; deterministic rules decide whether to search. External research runs only when one of these holds:

* the teacher explicitly asks for it;
* the model says research is needed and local knowledge isn't sufficient — meaning fewer than `SUFFICIENCY_MIN_HITS` strong vector hits, and the focus concepts aren't already supported by graph claims plus a relevant passage;
* nothing has been researched yet.

Searches go through a per-workspace cache keyed on the normalized query. Sources are deduplicated by canonical URL (lower-cased host, no `www.` or fragments, tracking parameters stripped, query parameters sorted) and by content hash. Graph extraction for new sources runs as a follow-up `graph_extract` job, so answers aren't blocked on rate-limited extraction calls.

**Grounding.**
* The answer may cite only the passage markers it was given. Unknown markers are stripped.
* URLs that don't belong to a retrieved source are replaced with "[link removed: not from a retrieved source]".
* General knowledge must be labelled "General background (not from researched sources)".
* Retrieved web content is wrapped in `<untrusted_*>` tags under an explicit policy, with the closing-tag escape neutralized. Model output is only ever parsed as schema-validated JSON and never executed.

## Roadmap, coverage, gaps

* **Roadmap.** A roadmap is a versioned view of the graph, not the graph itself. The LLM picks an organization (prerequisite, chronological, thematic, causal or mixed) from the graph outline and its key relations. Concept items are linked to graph entity IDs.
  * Teacher edits create a new version, flagging `teacher_edited` on whatever changed, with optimistic concurrency via `expected_version`.
  * Regeneration merges edits forward: edited or excluded phases and items survive, and orphaned edited items move to a "Teacher additions" phase.
  * Phase navigation covers start, next, previous, go to, skip, complete and revisit. It updates phase status and the session's current phase.
* **Coverage.** Each concept has independent timestamps for planned, researched, explained, discussed and completed, where completed is set only explicitly by the teacher.
* **Gaps.** The gap report distinguishes:
  * `required` gaps: graph PREREQUISITE_OF edges into planned concepts, items with no evidence, and contested claims;
  * `recommended` gaps: planner-declared prerequisites, single-source concepts, missing examples or notes, unverified claims and unresolved questions;
  * `optional` extensions: well-connected concepts that aren't planned.

  It reports factual counts, never mastery percentages.

## Assessment pipeline

1. **Scope.**
   * *Strict:* concepts covered at least to "researched", or concepts in the selected notes, and only those with evidence passages. Requested topics that weren't covered are excluded and reported.
   * *Expanded:* covered concepts plus their researched graph neighbours. Questions may apply them to new scenarios but must not introduce unresearched topics; mentions of out-of-scope concepts are flagged.
2. **Deterministic blueprint.** Sections and slots, each with type, marks, difficulty and target concept, built by largest-remainder allocation of the difficulty and topic distributions. Inconsistent totals are rejected with a 422 before any generation runs.
3. **Generation** in batches of up to 8 slots, using concept facts and evidence passages.
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
  * extractions: `(workspace, batch_key)`;
  * graph: MERGE on stable IDs;
  * artifacts: `(workspace, content fingerprint)`;
  * chat turns: an existing reply is detected and reused.
* **Partial failure.** A failed research query is recorded and the turn continues. A failed source ingest is skipped and reported. Store-specific steps (index, graph) are stamped per source and can be replayed.

## Security

* Workspace authorization runs on every route. Another user's workspace returns 404 rather than 403, to avoid enumeration. Graph and vector queries are always workspace-scoped, and chunk hydration re-checks ownership.
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
