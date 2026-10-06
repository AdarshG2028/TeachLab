# Implementation checklist

Legend:
* ✅ implemented and verified by automated tests or the demo run;
* ⚠️ implemented, with a documented limitation;
* ⏭️ not done, with the reason.

## Phase 1: Foundation
- ✅ Repo structure, `.gitignore`, `.env.example`, docker-compose (PostgreSQL 16, Qdrant)
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

## Phase 3: Knowledge graph (removed)
- ⏭️ Removed at the user's request after live use. Graph extraction used most of the free-tier Groq budget and lagged
  far behind (e.g. empty after a full PyTorch session), while lessons, notes and tests were built from source
  passages anyway. Neo4j, extraction, the ontology resolver, claims/contradictions and the Review/Graph tabs are gone
  (migration `remove_knowledge_graph_tables`).
- ✅ Replaced by a passage-based coverage check (`retrieval.relevant_for_concept`: similarity + key-term mention), used
  for "researched" coverage, gaps, lesson research, notes and test scope. Provenance remains via citations.

## Phase 4: Conversation and research agent
- ✅ Full message persistence, paging, multiple sessions, resume via the workspace overview
- ✅ Rolling summary, unsummarized-message window, token-budgeted context builder (`tests/unit/test_pure_logic.py`, `test_long_conversation_is_summarized_not_truncated`)
- ✅ Plan → sufficiency decision (vector hits and concept coverage) → cached search → ingest → index → grounded answer → state update
- ✅ Citation and URL grounding; untrusted-content wrapping (`test_grounding_strips_invented_citations_and_urls`)
- ✅ Explicit preferences, self-reported understanding kept separate from completion

## Phase 5: Familiarity workflows, roadmap, coverage, gaps
- ✅ Levels 0, 1 and 2 as default workflows, switchable without losing data (`test_workspace_crud_and_mode_switch`)
- ✅ Roadmap generation from research passages; versioning; teacher edits (reorder, include/exclude, edit, add) preserved across regeneration; single-phase regeneration
- ✅ Phase progression: start, next, previous, go to, skip, complete, revisit (`test_level0_beginner_tutorial_flow`)
- ✅ Coverage stages tracked separately (planned, researched, explained, discussed, completed)
- ✅ Gap report (required, recommended, optional, info) with factual counts and suggested research

- ✅ Guided learning flow (`test_guided_flow.py`, 9 tests). Verified live on a cuda-oxide topic with Groq + Exa:
  - **start** builds a researched roadmap that is shown in chat;
  - **next** delivers one structured lesson per phase, researching only uncovered concepts;
  - after the last phase, **generate teaching pack** produces 5 PDFs in one job;
  - phase progress bar and buttons in the UI;
  - interactive jobs are prioritised over background jobs.

## Phase 6: Notes and assessments
- ✅ Teacher and student notes from the passages that discuss each concept, coverage and conversation summaries; references only to real sources; versioned editing
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
- ✅ Next.js UI covering every workflow screen: workspaces and resume, setup, mode, chat with guided phase bar, roadmap, sources, coverage and gaps, notes preview and editing, test configuration, preview and editing, exports, review
- ✅ Typed client generated from OpenAPI; SSE job progress with polling fallback (`frontend/src/test`)

## Phase 8: Hardening and docs
- ✅ Durable jobs: retries with backoff, non-retryable failures, lease recovery, idempotency keys, worker re-checks of ownership
- ✅ Security tests: cross-user access (404 on every route family), SSRF, uploads, production guards
- ✅ README, ARCHITECTURE, DEPLOYMENT, demo script (`backend/scripts/demo_workflow.py`)
- ✅ Backend Docker image built (3.2 GB, CPU-only torch, no CUDA) and run as in production: `ENV=production`, anonymous auth, worker inside the API, files in Postgres. Verified live: roadmap, teaching pack with 5 PDFs, PDF download, CORS allow/deny, cross-browser isolation. ⚠️ The frontend image and an actual Render/Vercel deployment were not tested.
- ✅ Smoke-tested as separate processes (uvicorn + worker + `next start`, server Qdrant): chat turn over SSE, CORS, cross-user 404.
- ✅ UI rendered in headless Edge: home, chat (guided bar), roadmap, notes (KaTeX), tests. ⚠️ Interactive clicks (form submits, edits) were not exercised in a browser; the API paths behind them are covered by integration tests.
- ✅ Live provider tests: 5 passed (Groq ×4, Exa). Tavily skipped (no key). Run `uv run pytest -m live tests/live` and `scripts/demo_workflow.py --live` after adding keys.
- ⏭️ Automated browser E2E (Playwright): not added, because browser downloads were impractical on this network. Screenshots were checked manually instead (see above).

## Verified results (last run)
- Backend: `uv run pytest`: **105 passed**, 5 live tests deselected. `uv run pytest -m live`: 5 skipped (no keys). `ruff`: clean.
- Frontend: `pnpm test`: **5 passed**. `tsc --noEmit`: clean. `next build`: succeeded.
- Offline demo (`scripts/demo_workflow.py`, levels 0 and 1): every step succeeded and wrote 5 PDFs.

## Public deployment (no sign-in)
- ✅ Anonymous per-browser key instead of accounts; access-key copy/restore UI; usage caps (per user, global, workspaces); Postgres file storage; worker-in-API mode; provider DB-URL and CORS parsing; `render.yaml`; `docs/DEPLOY_VERCEL_RENDER.md`. Tests: `tests/integration/test_public_deploy.py` (12), frontend 9.
