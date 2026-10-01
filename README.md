# TeachLab

An AI research workspace for teachers. A teacher researches a subject, and the system builds a source-backed knowledge graph from the research. It adapts to how familiar the teacher is with the subject, and produces teaching roadmaps, teacher and student notes, validated test papers, answer keys and PDFs from what was actually researched.

* **Backend:** FastAPI, PostgreSQL, Neo4j, Qdrant, and local BGE embeddings. Groq is the LLM, Tavily is the search API, and PDFs are rendered locally with ReportLab.
* **Frontend:** Next.js, React, TypeScript and Tailwind. This is a functional, replaceable interface that covers the whole workflow.

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
  app/providers/         LLM (Groq, fake), search (Tavily, fixtures), embeddings (BGE), vector (Qdrant),
                         graph (Neo4j), PDF (ReportLab), storage (local FS)
  app/services/          research agent, ingestion, retrieval, knowledge (extraction + ontology),
                         conversation memory, roadmap + coverage/gaps, notes, assessment, export
  migrations/            Alembic migrations
  scripts/               demo_workflow.py (end-to-end demo), export_openapi.py
  tests/                 unit/, integration/ (docker services), live/ (real APIs), fixtures/
frontend/                Next.js app (src/app pages, src/components panels, src/lib typed API client)
docker-compose.yml       PostgreSQL 16, Neo4j 5 Community, Qdrant
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
docker compose up -d            # postgres:5432, neo4j:7687 (browser :7474), qdrant:6333

# 3. Backend
cd backend
uv sync --extra otel            # installs deps incl. CPU torch + sentence-transformers
uv run alembic upgrade head     # PostgreSQL schema
#   Neo4j constraints/indexes and the Qdrant collection are created automatically on worker start
#   (Container.graph.ensure_schema / vectors.ensure_collection). The first worker start downloads
#   BAAI/bge-small-en-v1.5 (~130 MB) from Hugging Face.

# 4. Run (two terminals)
uv run uvicorn app.main:app --reload --port 8000     # API  -> http://localhost:8000/docs
uv run python -m app.worker                          # worker (research, graph, notes, tests, PDFs)

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

## End-to-end demo

The demo creates a topic and runs research, the graph update, the roadmap, the Level-0 phase tutorial, notes, assessment configuration, the answer key and PDF downloads. It goes through the real API and worker.

```bash
cd backend
uv run python scripts/demo_workflow.py --topic "quantum computing qubits" --level 0      # offline
uv run python scripts/demo_workflow.py --live --topic "Photosynthesis" --level 1          # Groq + Tavily
# PDFs: backend/demo_output/<workspace-id>/{teacher_notes,student_notes,question_paper,answer_key,student_pack}.pdf
```

## Tests

```bash
cd backend
docker compose up -d                       # integration tests use the teachlab_test database + Neo4j
uv run pytest                              # unit + integration (live tests excluded by default)
uv run pytest -m nodb                      # only pure unit tests (no services needed)
uv run pytest -m live tests/live           # real Groq/Tavily calls; skipped when keys are missing
uv run ruff check app tests

cd ../frontend
pnpm test                                  # vitest (API client, form, job progress)
pnpm lint                                  # TypeScript type check
pnpm build
```

Integration tests run against the docker-compose PostgreSQL (`teachlab_test`) and Neo4j, an in-memory Qdrant and the real local BGE model. Every test removes the data it creates. The LLM and web search are replaced by a deterministic fake LLM and fixture search. These tests therefore verify orchestration, validation, persistence, isolation and provenance; they do not verify the quality of real model output, which the `live` tests and the `--live` demo cover.

## Credentials

| Variable | Where to get it | Notes |
|---|---|---|
| `GROQ_API_KEY` | https://console.groq.com/keys | Free tier. Check https://console.groq.com/docs/models for current model IDs and rate limits, and set `LLM_MODEL_SMART`, `LLM_MODEL_FAST` and `LLM_TOKENS_PER_MINUTE` to match. |
| `TAVILY_API_KEY` | https://app.tavily.com | The free plan includes a monthly credit allowance. Each search and extract call uses credits, and results are cached per workspace (`SEARCH_CACHE_TTL_HOURS`). |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | Local docker, or [Neo4j AuraDB Free](https://neo4j.com/cloud/aura-free/) | AuraDB URI format: `neo4j+s://<id>.databases.neo4j.io`. |
| `QDRANT_URL` / `QDRANT_API_KEY` | Local docker, or [Qdrant Cloud](https://cloud.qdrant.io) free cluster | Leave `QDRANT_URL` empty to use embedded mode (`QDRANT_PATH`). |
| `DATABASE_URL` | Local docker, or any PostgreSQL 14+ (Neon, Supabase, RDS…) | Must use the `postgresql+asyncpg://` scheme. |

Authentication is local and header-based in this MVP. `AUTH_PROVIDER=local` takes the user from an `X-User-Id` header, and the UI has a user switcher. The app refuses to start this way when `ENV=production`. A Clerk/JWT provider plugs in through `app/auth/base.py::AuthProvider`. Workspace authorization is enforced on every route either way.

## Known limitations

* **Authentication.** Clerk is not integrated (the user chose to skip auth for now), so local header auth is development-only.
* **Real model quality.** It is not verified without keys. The Groq and Tavily integrations are implemented and tested against mocked HTTP. Live tests exist but were skipped in this environment.
* **PDF math.** Math uses matplotlib *mathtext*: fractions, roots, sums, integrals, Greek letters and sub/superscripts are supported. `align`, `matrix` and `cases` environments print as LaTeX source, and are logged.
* **PDF fonts and diagrams.** The bundled DejaVu fonts have no CJK glyphs, so Chinese, Japanese and Korean text needs a CJK TTF registered in the PDF provider. Mermaid diagrams render in the web preview only; in PDFs they appear as source code.
* **Scanned PDFs.** Scanned PDFs without a text layer are rejected, because OCR is not included.
* **Generated code questions.** These are not executed unless `CODE_VERIFICATION_ENABLED=true`, and they are flagged for teacher review instead. Only enable that setting inside an isolated worker container.
* **Difficulty.** Difficulty matching is heuristic: the model's own rating is compared with the blueprint and mismatches are flagged.
* **Contradiction detection.** This is rule-based: an explicit negation of the same relation, or single-valued relations with conflicting objects. Subtler disagreements depend on the extraction model marking perspectives.
* **Chat responses.** Chat replies are delivered when the turn finishes, with progress reported per step over SSE. Token streaming is not implemented.
