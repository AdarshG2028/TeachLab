# TeachLab

An AI research workspace for teachers. A teacher researches a subject; the system keeps the sources, adapts to how familiar the teacher is with the subject, and produces teaching roadmaps, step-by-step lessons, teacher and student notes, validated test papers, answer keys and PDFs, all grounded in (and citing) what was actually researched.

* **Backend:** FastAPI, PostgreSQL, Qdrant, and local BGE embeddings. Groq is the LLM, Exa or Tavily is the web search API, and PDFs are rendered locally with ReportLab.
* **Frontend:** Next.js, React, TypeScript and Tailwind. This is a functional, replaceable interface that covers the whole workflow.

**Deploying?** See [docs/DEPLOY_VERCEL_RENDER.md](docs/DEPLOY_VERCEL_RENDER.md) (Vercel + Render, no sign-in, usage limits).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for deployment and free-tier notes, and [IMPLEMENTATION_CHECKLIST.md](IMPLEMENTATION_CHECKLIST.md) for status.

---

## Repository layout

```
backend/                 FastAPI app, worker, migrations, tests (uv project, Python 3.12-3.13)
  app/api/               HTTP routers + typed request/response schemas (OpenAPI at /docs)
  app/auth/              AuthProvider interface + local dev provider
  app/core/              settings, logging, errors, ids, retries, token counting
  app/db/                SQLAlchemy models (PostgreSQL = source of truth), sessions
  app/jobs/, app/worker/ durable PostgreSQL job queue + worker process + job handlers
  app/llm/               structured (JSON-schema validated) generation, prompt-injection wrapping
  app/providers/         LLM (Groq, fake), search (Exa, Tavily, fixtures), embeddings (BGE), vector (Qdrant),
                         PDF (ReportLab), storage (local FS)
  app/services/          research agent, guided tutor, ingestion, retrieval,
                         conversation memory, roadmap + coverage/gaps, notes, assessment, export
  migrations/            Alembic migrations
  scripts/               demo_workflow.py (end-to-end demo), export_openapi.py
  tests/                 unit/, integration/ (docker services), live/ (real APIs), fixtures/
frontend/                Next.js app (src/app pages, src/components panels, src/lib typed API client)
docker-compose.yml       PostgreSQL 16, Qdrant
infra/                   database init (creates the test database)
```

## Prerequisites

* Docker Desktop
* Python 3.12 or 3.13, and [uv](https://docs.astral.sh/uv/)
* Node 20 or newer, and pnpm
* About 2 GB free RAM for the worker (local BGE embeddings run on the CPU; no GPU needed)

## Local setup

```bash
# 1. Configuration
cp .env.example .env            # then add GROQ_API_KEY and TAVILY_API_KEY (see "Credentials")

# 2. Infrastructure
docker compose up -d            # postgres:5432, qdrant:6333

# 3. Backend
cd backend
uv sync --extra otel            # installs deps incl. CPU torch + sentence-transformers
uv run alembic upgrade head     # PostgreSQL schema
#   The Qdrant collection is created automatically on worker start. The first worker start downloads
#   BAAI/bge-small-en-v1.5 (~130 MB) from Hugging Face.

# 4. Run (two terminals)
uv run uvicorn app.main:app --reload --port 8000     # API  -> http://localhost:8000/docs
uv run python -m app.worker                          # worker (research, lessons, notes, tests, PDFs)

# 5. Frontend (third terminal)
cd ../frontend
pnpm install
cp .env.local.example .env.local                     # NEXT_PUBLIC_API_URL=http://localhost:8000
pnpm dev                                             # http://localhost:3000
```

**Running without API keys.** Set `LLM_PROVIDER=fake` and `SEARCH_PROVIDER=fixture` in `.env`. The whole workflow then runs offline with deterministic stand-ins. Search results come from fixtures covering four subjects: quantum computing, cloud computing, IoT and the French Revolution. Both settings are refused when `ENV=production`.

### Regenerating the typed frontend client

```bash
cd backend && uv run python scripts/export_openapi.py ../frontend/openapi.json
cd ../frontend && npx openapi-typescript openapi.json -o src/lib/api-types.ts
# or, with the API running:  pnpm gen:api
```

## Guided learning (recommended for new topics)

Create a workspace with familiarity **Beginner** and type **start** in the research chat, or press the
button above the chat:

1. You get a researched **roadmap** of 4-7 phases.
2. **next** teaches each phase as a lesson, researching only what isn't covered yet, and shows "Phase n of m".
3. At the end, **generate teaching pack** creates teacher and student notes, a question paper and a separate
   answer key as PDFs, with download links right in the chat.

Every lesson, note and question cites the real sources it was built from.

## End-to-end demo

The demo creates a topic and runs research, the roadmap, the Level-0 phase tutorial, notes, assessment configuration, the answer key and PDF downloads. It goes through the real API and worker.

```bash
cd backend
uv run python scripts/demo_workflow.py --topic "quantum computing qubits" --level 0      # offline
uv run python scripts/demo_workflow.py --live --topic "Photosynthesis" --level 1          # Groq + Exa/Tavily
# PDFs: backend/demo_output/<workspace-id>/{teacher_notes,student_notes,question_paper,answer_key,student_pack}.pdf
```

## Tests

```bash
cd backend
docker compose up -d                       # integration tests use the teachlab_test database
uv run pytest                              # unit + integration (live tests excluded by default)
uv run pytest -m nodb                      # only pure unit tests (no services needed)
uv run pytest -m live tests/live           # real Groq/Exa/Tavily calls; skipped when keys are missing
uv run ruff check app tests

cd ../frontend
pnpm test                                  # vitest (API client, form, job progress)
pnpm lint                                  # TypeScript type check
pnpm build
```

Integration tests run against the docker-compose PostgreSQL (`teachlab_test`), an in-memory Qdrant and the real local BGE model. Every test removes the data it creates. The LLM and web search are replaced by a deterministic fake LLM and fixture search. These tests therefore verify orchestration, validation, persistence, isolation and provenance; they do not verify the quality of real model output, which the `live` tests and the `--live` demo cover.

## Credentials

| Variable | Where to get it | Notes |
|---|---|---|
| `GROQ_API_KEY` | https://console.groq.com/keys | Free tier. Defaults are `openai/gpt-oss-120b` (smart) and `openai/gpt-oss-20b` (fast), since new free accounts may not have Llama models. List what your key can use with `GET https://api.groq.com/openai/v1/models`. Set `LLM_TOKENS_PER_MINUTE` to your plan's limit (free tier ≈ 8000). |
| `EXA_API_KEY` (with `SEARCH_PROVIDER=exa`) | https://dashboard.exa.ai/api-keys | Default search provider. Free credits at sign-up. Results are cached per workspace (`SEARCH_CACHE_TTL_HOURS`). |
| `TAVILY_API_KEY` (with `SEARCH_PROVIDER=tavily`) | https://app.tavily.com | Alternative search provider with a monthly free credit allowance. |
| `QDRANT_URL` / `QDRANT_API_KEY` | Local docker, or [Qdrant Cloud](https://cloud.qdrant.io) free cluster | Leave `QDRANT_URL` empty to use embedded mode (`QDRANT_PATH`). |
| `DATABASE_URL` | Local docker, or any PostgreSQL 14+ (Neon, Supabase, RDS…) | Must use the `postgresql+asyncpg://` scheme. |

There is no sign-in. Each browser holds a private random key (see the *Access key* box on the home page) and workspaces belong to whoever holds it; `AUTH_PROVIDER=anonymous` is safe for production. `AUTH_PROVIDER=local` (an `X-User-Id` header) is for tests only and is refused when `ENV=production`. Workspace authorization is enforced on every route either way.

## Known limitations

* **No knowledge graph.** The Neo4j knowledge graph (entities, claims, contradiction detection, ontology review) was removed: it used most of the free-tier LLM budget and the outputs did not depend on it. Source traceability remains through citations; contradiction detection between sources is no longer done.

* **Authentication.** There are no accounts: access is by a private browser key, which cannot be recovered if lost. Public sites should keep the usage limits on and consider an access code or real login.
* **Real model quality.** It is not verified without keys. The Groq and Tavily integrations are implemented and tested against mocked HTTP. Live tests exist but were skipped in this environment.
* **PDF math.** Math uses matplotlib *mathtext*: fractions, roots, sums, integrals, Greek letters and sub/superscripts are supported. `align`, `matrix` and `cases` environments print as LaTeX source, and are logged.
* **PDF fonts and diagrams.** The bundled DejaVu fonts have no CJK glyphs, so Chinese, Japanese and Korean text needs a CJK TTF registered in the PDF provider. Mermaid diagrams render in the web preview only; in PDFs they appear as source code.
* **Scanned PDFs.** Scanned PDFs without a text layer are rejected, because OCR is not included.
* **Generated code questions.** These are not executed unless `CODE_VERIFICATION_ENABLED=true`, and they are flagged for teacher review instead. Only enable that setting inside an isolated worker container.
* **Difficulty.** Difficulty matching is heuristic: the model's own rating is compared with the blueprint and mismatches are flagged.
* **Contradiction detection.** This is rule-based: an explicit negation of the same relation, or single-valued relations with conflicting objects. Subtler disagreements depend on the extraction model marking perspectives.
* **Chat responses.** Chat replies are delivered when the turn finishes, with progress reported per step over SSE. Token streaming is not implemented.
* **Not verified here.** Docker images were written but not built. The UI was rendered in headless Edge but its interactive flows weren't clicked through in a browser. Live Groq/Tavily calls were not run.
* **Request size.** Each LLM request is capped at `LLM_TOKENS_PER_MINUTE`, since Groq rejects any single request above the plan's TPM. With the free-tier default of 8000, prompts carry fewer passages per call. Raise it to your plan's real limit for richer context.
