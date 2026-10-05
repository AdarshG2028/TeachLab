# Implementation checklist

Legend:
* ✅ implemented and verified by automated tests or the demo run;
* ⚠️ implemented, with a documented limitation;
* ⏭️ not done, with the reason.

## Phase 1: Foundation
- ✅ Repo structure, `.gitignore`, `.env.example`, docker-compose (PostgreSQL 16, Neo4j 5, Qdrant)
- ✅ Settings with production guards (`app/core/config.py`)
- ✅ SQLAlchemy models plus an Alembic migration (`migrations/versions/*_initial_schema.py`)
- ⚠️ Auth: `AuthProvider` interface and a dev-only local header provider. Clerk was skipped at the user's request.
- ✅ Workspace CRUD with ownership authorization on every route (`tests/integration/test_api_and_jobs.py`)

## Phase 2: Providers
- ✅ Groq via the OpenAI SDK: tier routing, JSON mode, token-bucket throttle, retry-after handling, error mapping (`tests/unit/test_providers.py`)
- ✅ Request size capped to `LLM_TOKENS_PER_MINUTE`: budgets shrink to fit, oversized requests fail with a clear message, and the whole workflow is tested at a 6K per-request limit (`test_full_workflow_fits_small_free_tier_request_limit`)
- ✅ Exa (default) and Tavily search providers over REST, with retries and quota errors (mocked HTTP tests). Exa live-tested with the user's key (live suite: 5 passed, Tavily skipped).
- ✅ Groq live-tested with the user's key: 4 live tests pass on `openai/gpt-oss-120b` / `openai/gpt-oss-20b`, covering extraction for three subjects with verified quotes, and turn planning.
- ✅ SSRF-safe fetcher, HTML (trafilatura) and PDF (pypdf) extraction, upload validation (`tests/unit/test_io_safety_and_pdf.py`)
- ✅ Deterministic chunking, URL canonicalization, content-hash deduplication
- ✅ Local BGE embeddings: singleton per process, normalized, query instruction, CPU (measured RAM and latency in `docs/DEPLOYMENT.md`)
- ✅ Qdrant collection per model and dimension, workspace-filtered search, idempotent upserts, reindex job

## Phase 3: Knowledge graph
- ✅ Neo4j constraints and indexes, parameterized Cypher only, allowlisted labels, generic `:REL` edges
- ✅ LLM extraction with a schema and repair loop; raw extractions stored in PostgreSQL
- ✅ Ontology resolver covering entities, relations, validation and new types (`tests/unit/test_ontology.py`, 34 tests):
  - entity normalization, aliases and acronyms
  - fuzzy duplicate and homonym proposals
  - relation synonyms, inverse phrases and passive voice
  - families, direction and category validation
  - new-type proposals
- ✅ Claims, evidence and provenance: multi-source evidence, verified quotes, perspectives, contradictions preserved (`tests/integration/test_knowledge_pipeline.py`)
- ✅ Idempotent writes, per-workspace advisory lock, graph rebuild from PostgreSQL without LLM calls
- ✅ Domain-agnostic pipeline tested on quantum computing, cloud computing, IoT and history
- ✅ Review API: proposals (accept or reject, merge entities, remap relation types, reject one side of a contradiction) and claim verification

## Phase 4: Conversation and research agent
- ✅ Full message persistence, paging, multiple sessions, resume via the workspace overview
- ✅ Rolling summary, unsummarized-message window, token-budgeted context builder (`tests/unit/test_pure_logic.py`, `test_long_conversation_is_summarized_not_truncated`)
- ✅ Plan → sufficiency decision (vector hits and graph support) → cached search → ingest → index → grounded answer → state update
- ✅ Citation and URL grounding; untrusted-content wrapping (`test_grounding_strips_invented_citations_and_urls`)
- ✅ Explicit preferences, self-reported understanding kept separate from completion

## Phase 5: Familiarity workflows, roadmap, coverage, gaps
- ✅ Levels 0, 1 and 2 as default workflows, switchable without losing data (`test_workspace_crud_and_mode_switch`)
- ✅ Roadmap generation from the graph; versioning; teacher edits (reorder, include/exclude, edit, add) preserved across regeneration; single-phase regeneration
- ✅ Phase progression: start, next, previous, go to, skip, complete, revisit (`test_level0_beginner_tutorial_flow`)
- ✅ Coverage stages tracked separately (planned, researched, explained, discussed, completed)
- ✅ Gap report (required, recommended, optional, info) with factual counts and suggested research

- ✅ Guided learning flow (`test_guided_flow.py`, 9 tests). Verified live on a cuda-oxide topic with Groq + Exa:
  - **start** builds a researched roadmap that is shown in chat;
  - **next** delivers one structured lesson per phase, researching only uncovered concepts;
  - after the last phase, **generate teaching pack** produces 5 PDFs in one job;
  - phase progress bar and buttons in the UI;
  - interactive jobs and LLM calls are prioritised over background graph building.

## Phase 6: Notes and assessments
- ✅ Teacher and student notes from graph facts, evidence passages, coverage and conversation summaries; references only to real sources; versioned editing
- ✅ Assessment configuration (types, counts, marks, sections, totals, difficulty and topic distributions, duration, custom instructions); inconsistent totals rejected before generation
- ✅ Strict and expanded scope (`test_strict_vs_expanded_scope`)
- ✅ Blueprint → generation → validation and repair:
  - MCQ answer keys, true/false values, blanks
  - numeric answers via a safe evaluator
  - rubric rescaling, exact and semantic duplicate detection
  - strict-mode grounding, difficulty flags
- ⚠️ Coding questions are flagged for teacher review. Execution is opt-in (`CODE_VERIFICATION_ENABLED`) and the code runner is tested.
- ✅ Question edit, remove, regenerate (must differ from the original) and add by hand, with a live validation report
- ✅ Separate answer key and marking scheme

## Phase 7: PDFs, storage, frontend
- ✅ ReportLab PDFs: header metadata, page X of Y, headings, tables, code highlighting, math (mathtext), Unicode (DejaVu). ⚠️ CJK text and `align`/matrix environments not rendered.
- ✅ Student notes, teacher notes, question paper (no answers unless explicitly requested), answer key, student pack. Exports are idempotent and stored behind `StorageProvider`.
- ✅ Next.js UI covering every workflow screen: workspaces and resume, setup, mode, chat, roadmap, graph explorer with provenance, sources, coverage and gaps, notes preview and editing, test configuration, preview and editing, exports, review
- ✅ Typed client generated from OpenAPI; SSE job progress with polling fallback (`frontend/src/test`)

## Phase 8: Hardening and docs
- ✅ Durable jobs: retries with backoff, non-retryable failures, lease recovery, idempotency keys, worker re-checks of ownership
- ✅ Security tests: cross-user access (404 on every route family), SSRF, uploads, production guards
- ✅ README, ARCHITECTURE, DEPLOYMENT, demo script (`backend/scripts/demo_workflow.py`)
- ⚠️ Dockerfiles are written but `docker build` was never run. The lockfile pins CPU-only torch on Linux, with no CUDA packages.
- ✅ Smoke-tested as separate processes (uvicorn + worker + `next start`, server Qdrant): chat turn over SSE, CORS, follow-up graph job, cross-user 404.
- ✅ UI rendered in headless Edge: home, chat, roadmap, knowledge graph, notes (KaTeX), tests. ⚠️ Interactive clicks (form submits, edits) were not exercised in a browser; the API paths behind them are covered by integration tests.
- ✅ Live provider tests: 5 passed (Groq ×4, Exa). Tavily skipped (no key). Run `uv run pytest -m live tests/live` and `scripts/demo_workflow.py --live` after adding keys.
- ⏭️ Automated browser E2E (Playwright): not added, because browser downloads were impractical on this network. Screenshots were checked manually instead (see above).

## Verified results (last run)
- Backend: `uv run pytest`: **105 passed**, 5 live tests deselected. `uv run pytest -m live`: 5 skipped (no keys). `ruff`: clean.
- Frontend: `pnpm test`: **5 passed**. `tsc --noEmit`: clean. `next build`: succeeded.
- Offline demo (`scripts/demo_workflow.py`, levels 0 and 1): every step succeeded and wrote 5 PDFs.
